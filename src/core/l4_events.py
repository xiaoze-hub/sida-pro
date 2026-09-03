# -*- coding: utf-8 -*-
"""L4 事件数据源(2026-09-01, 设计稿 §5): 把四类真实事件算成 K 线可标注的事件列表。

事件 ``kind`` 取值与前端 `InteractiveKline` 的 `KlineEventKind` **一一对应**:

| kind            | 含义         | 数据源                      |
|-----------------|--------------|-----------------------------|
| limit_up        | 涨停         | bars(K线自算, 阈值 ±9.8%)   |
| limit_down      | 跌停         | bars(同上)                  |
| split_cluster   | 拆单簇       | .tck 逐笔(连续同向小单)     |
| cancel_anomaly  | 撤单异常     | .tck 撤单记录               |
| dragon_tiger    | 龙虎榜       | wencai(thsdk)               |
| announcement    | 公告         | wencai(thsdk)               |
| unlock / 价位线 | 解套盘位     | **chip_distribution 标准接口** |

> **2026-09-01 修正(用户指出)**: 解套盘位初版用"历史成交量分价位累加"自己估
> 算套牢区, 那是近似值; 仓库已有标准筹码模块 `src/core/chip_distribution.py`
> (腾讯当日分价表优先 + 新浪历史分价兜底), 改为**直接复用标准接口, 不自算**。
>
> **2026-09-01 暂缓**: `my_trade`(我的买卖点)按用户要求**先不做** ——
> 交割单是账户级数据而 summary 接口不区分用户, 多用户会串号, 等接口透传
> user_id 后再接。前端 KIND_LABEL 保留映射, 后端暂不产出该事件。

## ⚠️ 诚实口径(红线)

每个函数在数据缺失 / 数据源不可用时**返回空列表/None**, 不伪造任何事件:
- 无 .tck 文件 → 无拆单簇/撤单异常事件
- wencai 不可用(`available=False`) → 无龙虎榜/公告事件
- 筹码接口取不到 → 无解套盘位价位线

事件 dict 结构: `{"date": "YYYY-MM-DD", "kind": <KlineEventKind>, "label": "中文短标"}`,
与前端 `KlineEvent` 类型一致, 缺失字段一律不补假值。

## 单位
金额 = 元, 成交量 = 股(项目硬约束)。.tck 的 vol 本身就是股。
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# .tck 文件定位复用 dark_split(同一套 PANWATCH_TCK_DIR 约定, 不重复实现)
from src.core.dark_split import find_tck_file  # noqa: E402

# --- 拆单簇阈值 ---
CLUSTER_MIN_TRADES = 5            # 一簇最少笔数: 少于 5 笔不算"簇"
CLUSTER_WINDOW_MS = 30_000        # 相邻笔时间窗口: 30 秒内
CLUSTER_PRICE_TICKS = 1           # 价格容差: ±1 个价位(0.01 元)
CLUSTER_MAX_SINGLE_AMT = 300_000.0  # 单笔 < 30 万(伪装成小单, 未触发明盘)
CLUSTER_MIN_TOTAL_AMT = 300_000.0   # 簇总额 >= 30 万(实质是一笔大单被拆开)

# --- 撤单异常阈值 ---
CANCEL_BIG_VOL_SHARES = 50_000    # 单笔撤单 >= 5 万股 → 异常
CANCEL_BURST_COUNT = 20           # 同一分钟内 >= 20 笔撤单 → 异常(集中撤单)

PRICE_TICK = 0.01                 # A 股最小价位(元)


# ---------------------------------------------------------------------------
# ① 拆单簇 (.tck)
# ---------------------------------------------------------------------------
def split_clusters(trades: Iterable[dict], date_: str) -> list[dict]:
    """.tck 逐笔 → 拆单簇事件。

    判定: **连续**若干笔同方向、价格相近(±1 价位)、时间密集(相邻 30s 内)的成交,
    且每笔金额都 < 30 万(单看是小单, 不触发明盘), 但簇内总额 >= 30 万
    (合起来是一笔大单) → 判定为大单被拆成小单。

    Args:
        trades: `parse_tck` 的 trades(dict 含 t/price/vol/dir/amt)
        date_:  事件日期(YYYY-MM-DD), 来自 .tck 文件名或调用方

    Returns:
        事件列表; 无簇 → [] (不编造)。
    """
    rows = sorted([t for t in (trades or []) if isinstance(t, dict)], key=lambda x: x.get("t") or 0)
    events: list[dict] = []
    if not rows:
        return events

    cluster: list[dict] = [rows[0]]

    def _close(cur: list[dict]):
        """收尾一簇: 满足阈值则产出事件。"""
        if len(cur) < CLUSTER_MIN_TRADES:
            return
        total_amt = sum(float(t.get("amt") or 0.0) for t in cur)
        if total_amt < CLUSTER_MIN_TOTAL_AMT:
            return
        # 每笔都必须小于明盘阈值, 否则这是"明摆着的大单", 不算拆单
        if any(float(t.get("amt") or 0.0) >= CLUSTER_MAX_SINGLE_AMT for t in cur):
            return
        total_vol = sum(int(t.get("vol") or 0) for t in cur)
        side = "买" if str(cur[0].get("dir") or "").upper().startswith("B") else "卖"
        price = float(cur[0].get("price") or 0.0)
        events.append({
            "date": date_,
            "kind": "split_cluster",
            "label": f"拆单簇({side})",
            "price": round(price, 4),
            "count": len(cur),
            "shares": total_vol,
            "amount": round(total_amt, 2),
        })

    for prev, cur in zip(rows, rows[1:]):
        same_dir = str(prev.get("dir") or "").upper()[:1] == str(cur.get("dir") or "").upper()[:1]
        near_price = abs(float(cur.get("price") or 0) - float(prev.get("price") or 0)) <= CLUSTER_PRICE_TICKS * PRICE_TICK + 1e-9
        in_window = (int(cur.get("t") or 0) - int(prev.get("t") or 0)) <= CLUSTER_WINDOW_MS
        if same_dir and near_price and in_window:
            cluster.append(cur)
        else:
            _close(cluster)
            cluster = [cur]
    _close(cluster)
    return events


# ---------------------------------------------------------------------------
# ② 撤单异常 (.tck)
# ---------------------------------------------------------------------------
def cancel_anomalies(cancels: Iterable[dict], date_: str) -> list[dict]:
    """.tck 撤单记录 → 撤单异常事件。

    两类异常:
      1. 单笔大撤单: 撤单量 >= 5 万股
      2. 集中撤单: 同一分钟内撤单笔数 >= 20 笔(按 HHMM 分桶)

    ⚠️ .tck 的 cancel 记录**没有价格字段**, 因此只能按"股数"判定,
    无法换算金额 —— 结果里不出现 amount 字段, 不编造。

    Args:
        cancels: `parse_tck` 的 cancels(dict 含 t/vol/target)
        date_:   事件日期(YYYY-MM-DD)

    Returns:
        事件列表; 无异常 → []。
    """
    rows = [c for c in (cancels or []) if isinstance(c, dict)]
    if not rows:
        return []

    events: list[dict] = []

    # (1) 单笔大撤单 → 按日聚合成一条事件
    # 2026-09-03 撤单重叠修复: 同一日多笔大撤单以前各产一条事件(最多 20 条),
    # 前端按事件画 marker, 同 date 下 N 个 marker 叠在同一根 K 线上完全重合
    # (神剑 002361 出现 7 个"撤大额撤单"摞成一摞)。改为按日聚合一条,
    # label 带笔数, shares 为合计, time 取最晚一笔。
    big = [c for c in rows if _safe_vol(c) >= CANCEL_BIG_VOL_SHARES]
    if big:
        total_shares = sum(_safe_vol(c) for c in big)
        latest = max(big, key=_t_sort_key)
        events.append({
            "date": date_,
            "kind": "cancel_anomaly",
            "label": f"大额撤单({len(big)}笔)" if len(big) > 1 else "大额撤单",
            "shares": total_shares,
            "count": len(big),
            "time": _ms_to_hms(latest.get("t")),
        })

    # (2) 集中撤单(按分钟分桶)
    buckets: dict[str, int] = {}
    for c in rows:
        key = _ms_to_hms(c.get("t"))[:5]  # "HH:MM"
        if key and key != "--":
            buckets[key] = buckets.get(key, 0) + 1
    for minute, cnt in sorted(buckets.items()):
        if cnt >= CANCEL_BURST_COUNT:
            events.append({
                "date": date_,
                "kind": "cancel_anomaly",
                "label": f"集中撤单({cnt}笔)",
                "time": minute,
                "count": cnt,
            })
    return events


def _safe_vol(c: Any) -> int:
    """撤单股数安全取值: 非法/缺失 → 0(不抛异常, 调用方按阈值过滤)。"""
    try:
        return int((c or {}).get("vol") or 0)
    except (TypeError, ValueError):
        return 0


def _t_sort_key(c: Any) -> int:
    """撤单时间排序键: 非法时间 → -1(排最前, 不影响取最晚一笔)。"""
    try:
        return int((c or {}).get("t") or 0)
    except (TypeError, ValueError):
        return -1


def _ms_to_hms(t: Any) -> str:
    """.tck 的 u32 时间(HHMMSSmmm) → 'HH:MM:SS'; 无法解析 → '--'(不编造)。"""
    try:
        v = int(t or 0)
        if v <= 0:
            return "--"
        return f"{v // 10_000_000:02d}:{(v // 100_000) % 100:02d}:{(v // 1_000) % 100:02d}"
    except (TypeError, ValueError):
        return "--"


# ---------------------------------------------------------------------------
# ③ 龙虎榜 / 公告 (wencai)
# ---------------------------------------------------------------------------
def _wencai_events(symbol: str, query_tpl: str, kind: str, label: str,
                   date_: str) -> list[dict]:
    """wencai 查询 → 事件; 数据源不可用或零命中 → [](不编造)。"""
    try:
        from src.web.api.wencai import run_wencai
    except Exception as e:  # pragma: no cover
        logger.debug("wencai 不可用: %s", e)
        return []
    try:
        resp = run_wencai(query_tpl.format(code=symbol))
    except Exception as e:  # noqa: BLE001
        logger.debug("wencai 查询失败 %s: %s", query_tpl, e)
        return []
    if not isinstance(resp, dict) or not resp.get("available"):
        return []
    rows = resp.get("rows") or []
    if not rows:
        return []
    # 命中即产出一条事件; 条数带进 label, 前端可展开
    return [{
        "date": date_,
        "kind": kind,
        "label": f"{label}({len(rows)})",
        "count": len(rows),
    }]


def dragon_tiger_events(symbol: str, date_: str) -> list[dict]:
    """龙虎榜事件(数据源: wencai; 不可用 → [])。"""
    return _wencai_events(symbol, "{code} 龙虎榜", "dragon_tiger", "龙虎榜", date_)


def announcement_events(symbol: str, date_: str) -> list[dict]:
    """公告事件(数据源: wencai; 不可用 → [])。"""
    return _wencai_events(symbol, "{code} 最新公告", "announcement", "公告", date_)


# ---------------------------------------------------------------------------
# ④ 解套盘位 —— 走**标准筹码分布接口**, 不自己估算
# ---------------------------------------------------------------------------
def chip_levels(tencent_code: str | None) -> dict | None:
    """解套盘位 / 筹码结构, 数据源: `src.core.chip_distribution` 标准接口。

    ⚠️ 2026-09-01 修正: 初版用"历史成交量分价位累加"自己估算套牢区, 那是**近似值**;
    仓库已有标准筹码模块 `chip_distribution.compute_near_term_chips()`(腾讯当日分价表
    优先, 新浪历史分价兜底), 直接复用, **不自算**。

    Args:
        tencent_code: 腾讯风格代码('sz002361' / 'sh600519')

    Returns:
        {peak_price, peak_ratio, cost_10/50/90, profit_ratio, concentration,
         cost_band:{low,high,ratio}, last_close, source, window_days}
        取不到 → None(调用方显式标"无数据", 不编造)。
    """
    if not tencent_code:
        return None
    try:
        from src.core.chip_distribution import compute_near_term_chips

        return compute_near_term_chips(tencent_code, days=10)
    except Exception as e:  # noqa: BLE001
        logger.debug("筹码分布取数失败 %s: %s", tencent_code, e)
        return None


def unlock_levels_from_chips(chips: dict | None) -> list[dict]:
    """标准筹码结果 → K 线可渲染的**解套盘位**价位线。

    语义(不做近似, 全部来自标准接口的真实分价数据):
      - 筹码峰(peak_price)      : 成交最密集的价位 → 密集区
      - 成本 90 分位(cost_90)   : 90% 筹码的成本上沿 → 上方套牢盘分界
      - 主力成本带(cost_band)   : 含峰值的 ±5% 区间(累计占比 ≥40%)

    Args:
        chips: `chip_levels()` 的返回; None → [](不编造)

    Returns:
        [{price, kind, label, ratio}]; 缺字段的价位线不产出。
    """
    if not chips or not isinstance(chips, dict):
        return []

    out: list[dict] = []

    peak = chips.get("peak_price")
    peak_ratio = chips.get("peak_ratio")
    if isinstance(peak, (int, float)) and peak > 0:
        label = "筹码峰"
        if isinstance(peak_ratio, (int, float)):
            label += f"(占{peak_ratio:.1f}%)"
        out.append({"price": round(float(peak), 4), "kind": "pressure",
                    "label": label, "ratio": peak_ratio})

    c90 = chips.get("cost_90")
    if isinstance(c90, (int, float)) and c90 > 0:
        out.append({"price": round(float(c90), 4), "kind": "pressure",
                    "label": "套牢分界(成本90%)"})

    band = chips.get("cost_band")
    if isinstance(band, dict):
        for key, name in (("low", "主力成本下沿"), ("high", "主力成本上沿")):
            v = band.get(key)
            if isinstance(v, (int, float)) and v > 0:
                out.append({"price": round(float(v), 4), "kind": "support",
                            "label": name, "ratio": band.get("ratio")})

    # 按价格升序, 前端画出来从下到上
    return sorted(out, key=lambda x: x["price"])
