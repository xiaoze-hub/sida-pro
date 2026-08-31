"""市场主线识别 (v0.3.0, 2026-08-24)。

设计目标: 当日截面从涨停池(sector/theme)反推市场真正在打的"主线"题材,
输出 Top20 主线排名 + 各主线成分股列表, 供前端 MarketMainlineCard 卡片使用。

输入: MarketSentimentCollector().get_limit_up_pool()
  字段约定(code/name/days 连板数/sector 板块名或 theme 题材名/amount 成交额)。
  wudao 源还会带 theme(开盘啦主类题材);sector 取 wudao.theme 优先, 否则 sector,
  最后回落到 industry 兜底。

聚合指标(按 sector/theme 分组):
  - limit_up_count  当日涨停家数
  - ge2_count       二板及以上家数(过滤一日游假主线)
  - max_boards      最高板(连板天数最大值, 决定空间高度)
  - boards_sum      总板数(连续板天数求和, 量级指标)
  - rungs           梯队档位数(不同连板层级数, e.g. 1/2/3/5 板都存在 → rungs=4)
  - leader          龙头股 (按 days 倒序 → amount 倒序 → name 正序, 取第 1)

主线分(score, 0-100):
  当日截面 rank 归一化(0-1)后线性加权:
    0.35 * rank(limit_up_count)
  + 0.25 * rank(max_boards)
  + 0.25 * rank(rungs)        # 梯队覆盖度
  + 0.15 * rank(ge2_count)    # 二板宽度
  然后 × 100, 保留 2 位小数。

宽基过滤(剔除"伪主线"):
  1) 黑名单(板块成员数过大或非题材): 融资融券 / 沪深股通 / 沪股通 / 深股通 / 北向 /
     昨日涨停 / 昨日连板 / 昨日首板 / 涨停 / 打板 / 强势 / 活跃股 等;
  2) 名称子串黑名单: 同花顺 / 标普 / 富时 / MSCI / 昨日 / 连续(防"连续涨停"等假主线)。

最小入榜门槛: 当日涨停 ≥ 3 家(< 3 标记为 "未入榜", 保留展示但不参与排名)。

容错:
  - 涨停池为空 → 返回空 mainlines + 空 unranked;
  - sector 字段缺失 → 归 "其他" 分组(默认过滤);
  - days/amount 非数 → 兜底为 1/0.0。
"""

from __future__ import annotations

import logging
import math
from typing import Iterable

logger = logging.getLogger(__name__)

# ──────────── 常量 ────────────
# 主线分权重(和为 1.0)
W_LIMITUP = 0.35
W_MAXBOARDS = 0.25
W_RUNGS = 0.25
W_GE2 = 0.15

# 最小涨停家数(不达标不参与排名)
MIN_LIMITUP_FOR_RANK = 3

# 黑名单(精确匹配, 板块成员数过大或非题材概念)
BROAD_SECTORS: set[str] = {
    "融资融券",
    "沪深股通",
    "沪股通",
    "深股通",
    "北向",
    "北向资金",
    "昨日涨停",
    "昨日连板",
    "昨日首板",
    "涨停",
    "打板",
    "强势股",
    "活跃股",
    "ST板块",
    "次新股",
}

# 名称子串黑名单(任一子串命中即过滤)
BROAD_NAME_KEYWORDS: tuple[str, ...] = (
    "同花顺",
    "标普",
    "富时",
    "MSCI",
    "昨日",
    "连续",
)

# 默认分组名(板块字段缺失的兜底, 一律不参与排名)
DEFAULT_GROUP = "其他"


def _to_int_days(v) -> int:
    """days 字段安全转 int(非数 → 0)。get_limit_up_pool 已规整为 int, 兜底防 mock。"""
    try:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            return 0
        return int(v)
    except (TypeError, ValueError):
        return 0


def _to_float_amount(v) -> float:
    """amount 字段安全转 float(非数 → 0.0)。成交额单位: 元。"""
    try:
        if v is None:
            return 0.0
        f = float(v)
        if math.isnan(f):
            return 0.0
        return f
    except (TypeError, ValueError):
        return 0.0


def _resolve_group_name(item: dict) -> str:
    """从涨停条目抽取分组名(题材 > 板块 > 兜底"其他")。

    wudao 源带 theme(开盘啦主类题材), 东财源带 sector(行业板块名),
    都可能为空。本函数优先 theme(题材粒度更细), 其次 sector。
    """
    theme = str(item.get("theme") or "").strip()
    if theme and theme != "无":  # wudao theme='无' 表示无题材归类, 回落 sector
        return theme
    sector = str(item.get("sector") or "").strip()
    if sector:
        return sector
    industry = str(item.get("industry") or "").strip()
    if industry:
        return industry
    return DEFAULT_GROUP


def _is_broad_sector(name: str) -> bool:
    """宽基过滤: 黑名单 OR 名称子串黑名单命中即视为非主线题材。

    同时过滤 DEFAULT_GROUP("其他"): 板块/题材字段全缺失的涨停条目归到这里,
    不构成有效题材概念, 不参与排名(防止脏数据占用卡片)。
    """
    if not name:
        return True  # 空名同样视为非题材(防御性)
    if name == DEFAULT_GROUP:
        return True
    if name in BROAD_SECTORS:
        return True
    for kw in BROAD_NAME_KEYWORDS:
        if kw in name:
            return True
    return False


def _pick_leader(group_items: list[dict]) -> dict | None:
    """挑龙头股: days 倒序 → amount 倒序 → name 正序, 取第 1。"""
    if not group_items:
        return None
    items = [x for x in group_items if x is not None]
    if not items:
        return None
    items_sorted = sorted(
        items,
        key=lambda p: (
            -_to_int_days(p.get("days")),
            -_to_float_amount(p.get("amount")),
            str(p.get("name") or ""),
        ),
    )
    return items_sorted[0]


def _aggregate_one(group_name: str, items: list[dict]) -> dict:
    """单个分组聚合: 涨停数 / 二板数 / 最高板 / 总板数 / 梯队 / 龙头。"""
    days_list = [_to_int_days(p.get("days")) for p in items]
    days_list = [d if d > 0 else 1 for d in days_list]  # 兜底 1, 涨停池 days 至少 1
    limit_up_count = len(items)
    ge2_count = sum(1 for d in days_list if d >= 2)
    max_boards = max(days_list) if days_list else 0
    boards_sum = sum(days_list)
    rungs = len({d for d in days_list})  # 不同连板层级数
    leader = _pick_leader(items)

    # 成分股精简(按 days 倒序, amount 倒序, 取前 12 个, 防响应膨胀)
    constituents = sorted(
        items,
        key=lambda p: (
            -_to_int_days(p.get("days")),
            -_to_float_amount(p.get("amount")),
            str(p.get("name") or ""),
        ),
    )[:12]

    return {
        "name": group_name,
        "limit_up_count": limit_up_count,
        "ge2_count": ge2_count,
        "max_boards": max_boards,
        "boards_sum": boards_sum,
        "rungs": rungs,
        "leader": {
            "code": str((leader or {}).get("code") or ""),
            "name": str((leader or {}).get("name") or ""),
            "days": _to_int_days((leader or {}).get("days")),
            "amount": _to_float_amount((leader or {}).get("amount")),
        },
        "constituents": [
            {
                "code": str(p.get("code") or ""),
                "name": str(p.get("name") or ""),
                "days": _to_int_days(p.get("days")),
                "amount": _to_float_amount(p.get("amount")),
            }
            for p in constituents
            if str(p.get("code") or "")
        ],
    }


def _rank_norm(values: list[float]) -> list[float]:
    """线性归一化到 [0, 1]。全员相同 → 全部 1.0(均匀给最高档)。

    与 percentile rank 不同: 这里用 "max 归一"(最大值=1, 最小值=0)。
    主线分计算需要"相对当日截面"的强弱, 不是分布排名, 所以用 max 归一更直观。
    """
    if not values:
        return []
    vmax = max(values)
    if vmax <= 0:
        return [0.0] * len(values)
    return [v / vmax for v in values]


def _score_groups(groups: list[dict]) -> list[dict]:
    """对入榜分组(limit_up_count ≥ MIN_LIMITUP_FOR_RANK)按四维 rank 加权打分。

    顺序按 score 降序, 同分按 limit_up_count 降序, 再按 max_boards 降序。
    不参与打分的分组不在此函数处理(由 aggregate_mainline 统一标记为 "未入榜")。
    """
    if not groups:
        return []
    rank_lu = _rank_norm([g["limit_up_count"] for g in groups])
    rank_mb = _rank_norm([float(g["max_boards"]) for g in groups])
    rank_rungs = _rank_norm([float(g["rungs"]) for g in groups])
    rank_ge2 = _rank_norm([g["ge2_count"] for g in groups])

    for i, g in enumerate(groups):
        score = (
            W_LIMITUP * rank_lu[i]
            + W_MAXBOARDS * rank_mb[i]
            + W_RUNGS * rank_rungs[i]
            + W_GE2 * rank_ge2[i]
        )
        g["score"] = round(score * 100.0, 2)

    groups.sort(
        key=lambda g: (-g["score"], -g["limit_up_count"], -g["max_boards"], g["name"]),
    )
    return groups


def aggregate_mainline(
    limit_up_pool: Iterable[dict] | None,
    top_n: int = 20,
) -> dict:
    """主入口: 涨停池 → 主线排名 TopN + 未入榜分组。

    返回结构:
      {
        "total_groups":   入榜前总分组数(含未入榜),
        "ranked_groups":  [{name, limit_up_count, ge2_count, max_boards,
                           boards_sum, rungs, score, leader, constituents}, ...],
        "unranked":       [{name, limit_up_count, ...}, ...]  # 涨停 < 3 或宽基过滤后
        "filter_stats":   {"broad_filtered": int, "below_min": int, "ranked": int}
        "note":           str  # 空池时提示
      }

    宽基过滤说明: 被 _is_broad_sector 命中的分组(融资融券/沪深股通/昨日涨停/MSCI 等)
    整体剔除(不进 ranked_groups 也不进 unranked), 防止噪声占用前端卡片空间。
    """
    pool = list(limit_up_pool) if limit_up_pool else []
    if not pool:
        return {
            "total_groups": 0,
            "ranked_groups": [],
            "unranked": [],
            "filter_stats": {"broad_filtered": 0, "below_min": 0, "ranked": 0},
            "note": "无涨停池数据(非交易日/数据源不可用)",
        }

    # ① 按 sector/theme 分组
    bucket: dict[str, list[dict]] = {}
    for item in pool:
        name = _resolve_group_name(item)
        bucket.setdefault(name, []).append(item)

    # ② 宽基过滤
    broad_filtered_count = 0
    filtered_bucket: dict[str, list[dict]] = {}
    for name, items in bucket.items():
        if _is_broad_sector(name):
            broad_filtered_count += 1
            continue
        filtered_bucket[name] = items

    # ③ 聚合 + 打分
    aggregated = [_aggregate_one(name, items) for name, items in filtered_bucket.items()]

    eligible: list[dict] = []
    below_min: list[dict] = []
    for g in aggregated:
        if g["limit_up_count"] >= MIN_LIMITUP_FOR_RANK:
            eligible.append(g)
        else:
            g["score"] = None  # 未参与排名
            below_min.append(g)

    # eligible 内按 score 排名, 只取 top_n
    ranked = _score_groups(eligible)
    ranked_top = ranked[: max(0, int(top_n))]

    # 未入榜按 limit_up_count 倒序, 最多 10 个(展示用)
    below_min.sort(key=lambda g: (-g["limit_up_count"], -g["max_boards"], g["name"]))
    below_min_trim = below_min[:10]

    return {
        "total_groups": len(filtered_bucket),
        "ranked_groups": ranked_top,
        "unranked": below_min_trim,
        "filter_stats": {
            "broad_filtered": broad_filtered_count,
            "below_min": len(below_min),
            "ranked": len(ranked_top),
        },
        "note": "",
    }
