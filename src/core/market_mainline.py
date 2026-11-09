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
from datetime import datetime
from typing import Iterable
from zoneinfo import ZoneInfo

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


def _to_float_amount(v) -> float | None:
    """amount 字段安全转 float(缺失/非数 → None)。成交额单位: 元。

    P1(audit-20260915): 缺失返回 None, 不编造成 0.0(0 是合法成交额, 与"无数据"不同)。
    """
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _amount_sort_key(v) -> float:
    """排序用: None 按 0 处理(仅用于比较, 输出仍保持 None)。"""
    a = _to_float_amount(v)
    return a if a is not None else 0.0


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
            -_amount_sort_key(p.get("amount")),
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
            -_amount_sort_key(p.get("amount")),
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


def _concept_names(item: dict) -> list[str]:
    """从涨停条目取**完整概念归属**(去空/去'无'/保序去重)。

    2026-11-09: TQ `get_relation` 返回 880xxx 概念全量; 采集侧不再截前 3,
    否则"商业航天"这类第 4+ 位归属的概念永远进不了主线(漏题材)。
    """
    raw = item.get("concepts")
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in raw:
        name = str(v or "").strip()
        if not name or name == "无" or name == DEFAULT_GROUP or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def _pool_as_of(pool: list[dict]) -> str | None:
    """池子的数据交易日: 取条目 `as_of` 的众数; 全缺 → None(不猜成今天)。"""
    counts: dict[str, int] = {}
    for item in pool:
        v = str(item.get("as_of") or "").strip()
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


def _pool_source(pool: list[dict]) -> str:
    """池子的主数据源(按条目 source 众数); 缺失 → 'unknown'。"""
    counts: dict[str, int] = {}
    for item in pool:
        v = str(item.get("source") or "").strip()
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return "unknown"
    return max(counts, key=lambda k: counts[k])


def _pool_as_of_basis(pool: list[dict]) -> str:
    """数据日的**可证实程度**: `kline`(K线末根日期) / `source_request`(按请求日推定) / `unknown`。"""
    counts: dict[str, int] = {}
    for item in pool:
        v = str(item.get("as_of_basis") or "").strip()
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return "unknown"
    return max(counts, key=lambda k: counts[k])


def _pool_coverage(pool: list[dict]) -> dict:
    """池子覆盖率(条目 `pool_coverage` 众数)。**缺失 → unknown, 绝不声称完整**。

    涨停家数只有在"全市场代码都取到日线"时才是完整口径; 分片失败/漏回的票会让
    家数偏低, 消费方必须能看出这是下限而不是全量。
    """
    counts: dict[str, int] = {}
    detail: dict[str, dict] = {}
    for item in pool:
        cov = item.get("pool_coverage")
        if isinstance(cov, dict) and str(cov.get("status") or "").strip():
            st = str(cov["status"]).strip()
            counts[st] = counts.get(st, 0) + 1
            detail.setdefault(st, cov)
    if not counts:
        return {
            "status": "unknown",
            "requested_codes": None,
            "fetched_codes": None,
            "batch_failures": None,
            "note": "涨停池未提供覆盖率信息，无法确认是否覆盖全市场（未声称完整）",
        }
    status = max(counts, key=lambda k: counts[k])
    cov = detail.get(status) or {}
    if status == "complete":
        note = ""
    elif status == "partial":
        note = (
            f"涨停池未取全：{cov.get('fetched_codes')}/{cov.get('requested_codes')} 只取到日线"
            f"（{cov.get('batch_failures') or 0} 批失败），涨停家数为下限，不代表全市场完整池"
        )
    else:
        note = "涨停池未提供覆盖率信息，无法确认是否覆盖全市场（未声称完整）"
    return {
        "status": status,
        "requested_codes": cov.get("requested_codes"),
        "fetched_codes": cov.get("fetched_codes"),
        "batch_failures": cov.get("batch_failures"),
        "note": note,
    }


def _concept_as_of(pool: list[dict]) -> str | None:
    """概念/行业归属的反查日期(条目 `block_as_of` 众数); 全缺 → None(不猜成池子同日)。"""
    counts: dict[str, int] = {}
    for item in pool:
        v = str(item.get("block_as_of") or "").strip()
        if v:
            counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda k: counts[k])


def _dedupe_pool(pool: list[dict]) -> list[dict]:
    """按代码去重(先到保留)。池子可能因翻页/多源并集残留重复条目 ——
    重复条目会让家数虚高(同一只票计 2 家), 这里一处收敛, 后续所有计数复用。"""
    out: list[dict] = []
    seen: set[str] = set()
    for item in pool:
        code = str(item.get("code") or "").strip()
        if code:
            if code in seen:
                continue
            seen.add(code)
        out.append(item)
    return out


def _concept_meta(pool: list[dict], pool_source: str, tagged: int, pool_as_of: str | None = None) -> dict:
    """概念维度的可用性 / 来源标注(缺就明说, 不拿行业补, 不臆测来源)。"""
    total = len(pool)
    if tagged <= 0:
        status = "unavailable"
        note = (
            f"当前涨停池来源({pool_source})未提供概念字段，概念维度为空——"
            "这不等于「今日没有概念题材」；本卡片不拿行业/题材冒充概念。"
        )
    elif tagged < total:
        status = "partial"
        note = (
            f"{tagged}/{total} 只涨停股有概念归属，其余缺失（数据源未提供概念字段），"
            "概念家数可能偏低；缺失未被行业/题材补造。"
        )
    else:
        status = "available"
        note = ""

    # 来源: 优先条目自带的 block_source(采集侧写实); 无标记时只在 TQ 族池子上
    # 才敢说 tq:get_relation(TQ 是概念字段的唯一生产者), 其余一律 unknown。
    src_counts: dict[str, int] = {}
    for item in pool:
        v = str(item.get("block_source") or "").strip()
        if v and _concept_names(item):
            src_counts[v] = src_counts.get(v, 0) + 1
    if src_counts:
        source = max(src_counts, key=lambda k: src_counts[k])
        source_note = ""
    elif tagged > 0 and pool_source in ("tq", "tq_zdt"):
        source = "tq:get_relation"
        source_note = "概念字段未带来源标记，按 TQ 族涨停池推定"
    elif tagged > 0:
        source = "unknown"
        source_note = "概念字段缺少来源标记且涨停池非 TQ 族，未臆测来源"
    else:
        source = None
        source_note = ""

    # 归属时点(2026-11-09): 概念/行业是**反查当前成员关系**得来的, 而池子可能是上一
    # 交易日的 K 线。两者不同日时必须分开标注 —— 否则消费方会把"今天的归属"当成
    # "那天的归属"(同日伪装)。
    c_as_of = _concept_as_of(pool)
    if c_as_of is None:
        c_as_of_note = "概念归属未提供反查日期，归属时点未知（未猜成与池子同日）"
    elif pool_as_of and c_as_of != pool_as_of:
        c_as_of_note = (
            f"概念归属取当前（{c_as_of}）成员关系，与涨停池数据日（{pool_as_of}）不同日，"
            "不能当作该日的概念归属"
        )
    else:
        c_as_of_note = ""

    return {
        "status": status,
        "note": note,
        "source": source,
        "source_note": source_note,
        # 有来源标记才算"可核实"; 无标记时 source 是推定值
        "source_verified": bool(src_counts),
        "as_of": c_as_of,
        "as_of_note": c_as_of_note,
    }


def _aggregate_concepts(pool: list[dict]) -> tuple[list[dict], dict]:
    """概念**独立维度**聚合: 一只票可进多个概念, 各概念家数不可相加。

    返回 (groups, stats)。统计口径写死在 stats 里, 供前端/报告原样展示:
      · distinct_stocks   有概念归属的去重股票数
      · member_count_sum  各概念 limit_up_count 之和(会重复计同一只票)
      · overlap_excess    sum - distinct(重复计数次数, >0 即证明不可相加)
      · multi_concept_stocks 同时属于 ≥2 个概念的票数
      · summable          False —— 契约钉子, 消费方别把概念家数当唯一家数
    """
    stock_concepts: dict[str, list[str]] = {}  # code -> 本池内的非宽基概念
    for item in pool:
        code = str(item.get("code") or "")
        if not code:
            continue
        names = [n for n in _concept_names(item) if not _is_broad_sector(n)]
        if names:
            stock_concepts[code] = names

    bucket: dict[str, list[dict]] = {}
    broad_names: set[str] = set()
    for item in pool:
        code = str(item.get("code") or "")
        raw_names = _concept_names(item)
        if not raw_names:
            continue
        kept = stock_concepts.get(code)
        if not kept:
            broad_names.update(raw_names)  # 全部命中宽基黑名单
            continue
        for name in kept:
            bucket.setdefault(name, []).append(item)
        broad_names.update(n for n in raw_names if n not in kept)

    groups: list[dict] = []
    for name, items in bucket.items():
        # 同一概念内按代码去重(脏数据重复归属只算 1 只)
        uniq: dict[str, dict] = {}
        for it in items:
            uniq.setdefault(str(it.get("code") or ""), it)
        members = [v for k, v in uniq.items() if k]
        if not members:
            continue
        g = _aggregate_one(name, members)
        g["dimension"] = "concept"
        # 归属诚实: leader 只是"本概念内排序第一", 不是这只票的专属驱动
        leader_code = str((g.get("leader") or {}).get("code") or "")
        others = sorted(n for n in (stock_concepts.get(leader_code) or []) if n != name)
        g["leader_shared_concepts"] = others
        g["leader_basis"] = "本概念内连板数优先、成交额次之排序第一；非该股专属驱动"
        solo = sum(1 for it in members if len(stock_concepts.get(str(it.get("code") or ""), [])) <= 1)
        g["exclusive_ratio"] = round(solo / len(members), 2)
        for c in g["constituents"]:
            c["concept_count"] = len(stock_concepts.get(c["code"], []))
        groups.append(g)

    distinct_stocks = len(stock_concepts)
    member_count_sum = sum(g["limit_up_count"] for g in groups)
    multi = sum(1 for names in stock_concepts.values() if len(names) >= 2)
    stats = {
        "distinct_stocks": distinct_stocks,
        "tagged_stocks": distinct_stocks,
        "untagged_stocks": max(0, len(pool) - distinct_stocks),
        "multi_concept_stocks": multi,
        "member_count_sum": member_count_sum,
        "overlap_excess": max(0, member_count_sum - distinct_stocks),
        "overlap_ratio": round((member_count_sum - distinct_stocks) / member_count_sum, 2)
        if member_count_sum
        else 0.0,
        "broad_filtered": len(broad_names),
        "summable": False,
        "min_limitup_for_rank": MIN_LIMITUP_FOR_RANK,
        "legacy_payload": False,
        "note": "概念为独立维度：一只股票可同时属于多个概念，各概念涨停家数不可相加（相加会重复计同一只票）。",
    }
    return groups, stats


def aggregate_mainline(
    limit_up_pool: Iterable[dict] | None,
    top_n: int = 20,
    *,
    as_of: str | None = None,
    pool_source: str | None = None,
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
    pool = _dedupe_pool(list(limit_up_pool)) if limit_up_pool else []
    computed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    resolved_as_of = as_of or _pool_as_of(pool)
    resolved_source = pool_source or _pool_source(pool)
    # 概念独立维度先算(可用性标注要用它的去重计数; 空池分支不在此列)
    concept_all, concept_stats = _aggregate_concepts(pool)
    cmeta = _concept_meta(
        pool, resolved_source, concept_stats["tagged_stocks"], pool_as_of=resolved_as_of
    )
    coverage = _pool_coverage(pool)
    as_of_basis = _pool_as_of_basis(pool)
    data_meta = {
        "as_of": resolved_as_of,
        # 数据日怎么来的: K线证实 / 按请求日推定 / 未知 —— 消费方可据此决定能否当历史真值用
        "as_of_basis": as_of_basis,
        "as_of_verified": as_of_basis == "kline",
        "computed_at": computed_at,
        "pool_source": resolved_source,
        # 覆盖率(2026-11-09): 取数不全/无覆盖率信息时如实标出, 家数别被当全量
        "pool_coverage": coverage,
        "coverage_note": coverage["note"],
        "concept_source": cmeta["source"],
        "concept_source_note": cmeta["source_note"],
        "concept_source_verified": cmeta["source_verified"],
        # 概念/行业归属的反查日 ≠ 池子数据日时必须分开说(不许伪装同日)
        "concept_as_of": cmeta["as_of"],
        "concept_as_of_note": cmeta["as_of_note"],
        "concept_status": cmeta["status"],
        "concept_note": cmeta["note"],
        "as_of_note": ""
        if resolved_as_of
        else "涨停池未提供交易日字段，数据时点未知（未猜测为当日）",
        "dimensions": {
            "industry": "theme > sector > industry 单一归属（原有口径，未变）",
            "concept": "概念独立维度：一股多概念，各概念家数独立统计、不可相加；来源见 concept_source，缺失即空，不补造",
        },
    }
    if not pool:
        return {
            "total_groups": 0,
            "ranked_groups": [],
            "unranked": [],
            "filter_stats": {"broad_filtered": 0, "below_min": 0, "ranked": 0},
            # 涨停池只数(2026-09-23): 空态文案要能说"池子有 N 只、但没有板块够格",
            # 而不是一律赖"非交易日" —— 归因说错比不显示更糟。
            "total_limit_ups": 0,
            "min_limitup_for_rank": MIN_LIMITUP_FOR_RANK,
            "note": "无涨停池数据(非交易日/数据源不可用)",
            # 概念维度(2026-11-09): 空池同样回空结构, 契约不随数据有无而变
            "dimension": "industry",
            "concept_groups": [],
            "concept_unranked": [],
            "concept_status": "unavailable",
            "concept_note": "无涨停池数据（非交易日/数据源不可用），概念维度无输入，不代表今日没有概念题材。",
            "concept_stats": {
                "distinct_stocks": 0,
                "tagged_stocks": 0,
                "untagged_stocks": 0,
                "multi_concept_stocks": 0,
                "member_count_sum": 0,
                "overlap_excess": 0,
                "overlap_ratio": 0.0,
                "broad_filtered": 0,
                "summable": False,
                "min_limitup_for_rank": MIN_LIMITUP_FOR_RANK,
                "legacy_payload": False,
                "note": "无概念归属数据（涨停池为空或数据源未提供概念字段）",
            },
            "data_meta": data_meta,
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
    for g in aggregated:
        g["dimension"] = "industry"

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

    # ④ 概念独立维度(2026-11-09): 行业结果一条不丢, 概念另起一份排名。
    #    概念"一股多概念" ⇒ 各概念家数独立统计, 不可相加(concept_stats 里写明)。
    concept_eligible: list[dict] = []
    concept_below: list[dict] = []
    for g in concept_all:
        if g["limit_up_count"] >= MIN_LIMITUP_FOR_RANK:
            concept_eligible.append(g)
        else:
            g["score"] = None
            concept_below.append(g)
    concept_ranked = _score_groups(concept_eligible)[: max(0, int(top_n))]
    concept_below.sort(key=lambda g: (-g["limit_up_count"], -g["max_boards"], g["name"]))

    return {
        "total_groups": len(filtered_bucket),
        "ranked_groups": ranked_top,
        "unranked": below_min_trim,
        "filter_stats": {
            "broad_filtered": broad_filtered_count,
            "below_min": len(below_min),
            "ranked": len(ranked_top),
        },
        # 2026-09-23: 池子规模与入榜门槛都吐出来 —— 前端空态才能说清"33 只涨停散在 26 个板块,
        # 没有板块达到 ≥3 只门槛", 而不是笼统写"非交易日/涨停池为空"(归因错)。
        "total_limit_ups": len(pool),
        "min_limitup_for_rank": MIN_LIMITUP_FOR_RANK,
        "note": "",
        # ── 概念维度(2026-11-09, 纯新增键) ──
        "dimension": "industry",
        "concept_groups": concept_ranked,
        "concept_unranked": concept_below[:10],
        "concept_status": cmeta["status"],
        "concept_note": cmeta["note"],
        "concept_stats": concept_stats,
        "data_meta": data_meta,
    }
