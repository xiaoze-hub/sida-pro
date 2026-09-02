# -*- coding: utf-8 -*-
"""暗盘资金「通达信 .tck + 同花顺 thsdk」**融合** —— 决策先锋暗盘的完整解。

## 为什么要融合(单链路都不闭环)

官方暗盘 = 主力把大单**拆成小单**成交的资金(私募量化单 / 对倒 / 大单拆小单)。
要算出它必须同时满足两件事:

  1. **方向精确**  —— 谁在主动买、谁在主动卖
  2. **覆盖被动侧** —— 挂在盘口上被别人打掉的(maker)成交也算数

而两条链路各缺一半(2026-08-30 通达信研究结论 + 2026-09-02 同花顺盘中实测):

| 源 | 主动侧 | 被动侧 | 方向精度 |
|---|---|---|---|
| 通达信 `.tck` | ✅ **委托号级 100% 精确**(a28→主动买 / a32→主动卖, 委托量==成交量 99.8%) | ❌ **被动 maker 未落盘**(约占 23.2% 成交量, 格式固有局限) | 官方标记 2B/2S(交易所级) |
| 同花顺 thsdk `tick_super_level1` | 🟡 全量(含被动) | ✅ 覆盖 | 委托买入价/卖出价**推断**(启发式) |

→ **融合方案**: 主动侧取 `.tck` 真值, 被动侧由「同花顺全量 − 通达信主动」估计。
这是两条链路唯一能互补出完整暗盘的方式; 任一单链路都在原理上无法闭环。

## 硬规则(红线)

- 两侧**口径不同**(thsdk 是 3 秒累计条差分, .tck 是逐笔精确), 相减得到的被动侧
  **必然带误差** → 一律标 `estimated`; 且当被动侧占比落在合理区间外时标 `suspect`,
  **绝不冒充精确值**。
- 任一源不可达 → `coverage` 降级为只覆盖一侧, 返回可得部分, **不返回 0**。
- 金额 = 元, 成交量 = 股。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# 拆单簇阈值(与 dark_flow._detect_split_orders v4 同口径)
SPLIT_GAP_MS = 3_000         # 相邻委托时间窗口: 3 秒内
SPLIT_MIN_CLUSTER_AMT = 3e5  # 簇累计 >= 30 万 → 视为"大单被拆成小单"
SPLIT_PRICE_TICK = 0.02      # 价格容差(元)

# 被动侧占比合理区间(经验值: 主动约 76.7%, 被动约 23.2%)
PASSIVE_RATIO_LOW = 0.05
PASSIVE_RATIO_HIGH = 0.60


def _tencent_code(symbol: str) -> Optional[str]:
    s = (symbol or "").strip().lower()
    if s.startswith(("sz", "sh", "bj")):
        return s
    if s.isdigit() and len(s) == 6:
        if s[0] in ("6", "9") or s.startswith("688"):
            return "sh" + s
        if s[0] in ("0", "2", "3"):
            return "sz" + s
    return None


def _fetch_tck(symbol: str) -> Optional[tuple[list[dict], list[dict], list[dict]]]:
    """解析 .tck → (trades, orders, cancels); 不可得返回 None。"""
    try:
        from src.core.dark_split import find_tck_file
        from src.core.tdx_tick_parser import parse_tck

        path = find_tck_file(symbol) or find_tck_file(symbol, None)
        if not path:
            return None
        return parse_tck(path)
    except Exception as e:  # noqa: BLE001
        logger.debug("fusion: .tck 解析失败 %s: %s", symbol, e)
        return None


def active_net_from_tck(trades: list[dict]) -> Optional[dict]:
    """通达信主动侧净额(官方方向 2B/2S, 精确)。

    Returns:
        {"net": 元, "buy": 元, "sell": 元, "count": n, "source": "tdx_tck",
         "confidence": "official_exact"}
    """
    if not trades:
        return None
    buy = sell = 0.0
    n = 0
    for t in trades:
        amt = t.get("amt")
        if not isinstance(amt, (int, float)) or amt <= 0:
            continue
        d = (t.get("dir") or "").upper()
        if d == "B":
            buy += amt
        elif d == "S":
            sell += amt
        else:
            continue
        n += 1
    if n == 0:
        return None
    return {
        "net": round(buy - sell, 2), "buy": round(buy, 2), "sell": round(sell, 2),
        "count": n, "source": "tdx_tck",
        "confidence": "official_exact",   # 交易所级方向标记, 非推断
    }


def split_clusters_from_orders(
    orders: list[dict],
    gap_ms: int = SPLIT_GAP_MS,
    min_cluster_amt: float = SPLIT_MIN_CLUSTER_AMT,
) -> dict:
    """**委托级**拆单识别(通达信独有): 同方向、时间密集、价格相近的连续委托 → 一簇。

    与 `dark_flow._detect_split_orders`(按成交时间聚类)的区别: 本函数聚的是
    **委托申报**(.tck type=1 tag"00", 含 a28/a32 主动买卖指向), 方向由委托号关联
    给出而非价格推断, 因此更贴近"主力把一笔大单拆成多笔小委托"的真实形态。

    Returns:
        {"net": 元, "buy": 元, "sell": 元, "clusters": [[委托...]], "count": n}
    """
    # 方向: a28 指向主动买成交 → 主买委托; a32 指向主动卖成交 → 主卖委托
    rows = []
    for o in orders or []:
        amt = o.get("amt")
        if not isinstance(amt, (int, float)) or amt <= 0:
            continue
        if o.get("a28"):
            d = "B"
        elif o.get("a32"):
            d = "S"
        else:
            continue
        rows.append({**o, "_d": d})
    if not rows:
        return {"net": 0.0, "buy": 0.0, "sell": 0.0, "clusters": [], "count": 0}

    rows.sort(key=lambda r: r.get("t") or 0)

    clusters: list[list[dict]] = []
    cur: list[dict] = [rows[0]]
    for prev, r in zip(rows, rows[1:]):
        same_dir = r["_d"] == prev["_d"]
        close_time = (r.get("t") or 0) - (prev.get("t") or 0) <= gap_ms
        close_price = abs((r.get("price") or 0) - (prev.get("price") or 0)) <= SPLIT_PRICE_TICK
        if same_dir and close_time and close_price:
            cur.append(r)
        else:
            clusters.append(cur)
            cur = [r]
    clusters.append(cur)

    buy = sell = 0.0
    hit: list[list[dict]] = []
    for c in clusters:
        total = sum(float(x.get("amt") or 0) for x in c)
        if total < min_cluster_amt:
            continue
        hit.append(c)
        if c[0]["_d"] == "B":
            buy += total
        else:
            sell += total

    return {
        "net": round(buy - sell, 2),
        "buy": round(buy, 2),
        "sell": round(sell, 2),
        "clusters": hit,
        "count": len(hit),
    }


def _thsdk_total_net(symbol: str) -> Optional[dict]:
    """同花顺全量逐笔净额(含被动侧), 方向由委托买卖价推断。"""
    code = _tencent_code(symbol)
    if not code:
        return None
    try:
        from src.core import dark_l2

        ticks = dark_l2.fetch_l2_ticks(code, "thsdk")
    except Exception as e:  # noqa: BLE001
        logger.debug("fusion: thsdk 逐笔失败 %s: %s", symbol, e)
        return None
    if not ticks:
        return None
    buy = sell = 0.0
    n = 0
    for t in ticks:
        amt = t.get("amt")
        if not isinstance(amt, (int, float)) or amt <= 0:
            continue
        d = (t.get("d") or "").upper()
        if d == "B":
            buy += amt
        elif d == "S":
            sell += amt
        else:
            continue  # M(中性, 含集合竞价保护)不计入多空
        n += 1
    if n == 0:
        return None
    return {"net": round(buy - sell, 2), "buy": round(buy, 2), "sell": round(sell, 2),
            "count": n, "source": "thsdk_tick_super_level1", "confidence": "l2_thsdk"}


def compute_dark_fusion(symbol: str) -> Optional[dict]:
    """**融合暗盘**: 通达信主动侧(精确) + 同花顺被动侧(估计)。

    Returns:
        {
          "symbol",
          "active": {...} | None,     # .tck 官方方向, confidence="official_exact"
          "split":  {...} | None,     # 委托级拆单簇(暗盘主体形态)
          "total":  {...} | None,     # thsdk 全量(含被动), confidence="l2_thsdk"
          "passive_est": float | None,# = total.net - active.net(估计)
          "passive_flag": "ok" | "suspect" | None,
          "coverage": "fusion" | "thsdk_only" | "tck_only" | "none",
          "note": str | None,
        }
        两源都不可得 → None(调用方按无数据处理)。
    """
    tck = _fetch_tck(symbol)
    trades, orders, _cancels = tck if tck else (None, None, None)

    active = active_net_from_tck(trades) if trades else None
    split = split_clusters_from_orders(orders) if orders else None
    total = _thsdk_total_net(symbol)

    if active is None and total is None:
        return None

    passive_est = None
    flag = None
    note = None
    if active is not None and total is not None:
        passive_est = round(total["net"] - active["net"], 2)
        turnover = abs(total["buy"]) + abs(total["sell"])
        if turnover > 0:
            ratio = abs(passive_est) / turnover
            # 口径差异(3 秒条差分 vs 逐笔精确)会让相减值失真, 越界即标 suspect
            flag = "ok" if PASSIVE_RATIO_LOW <= ratio <= PASSIVE_RATIO_HIGH else "suspect"
            if flag == "suspect":
                note = (f"被动侧估计占比 {ratio:.1%} 超出经验区间"
                        f"[{PASSIVE_RATIO_LOW:.0%}, {PASSIVE_RATIO_HIGH:.0%}], "
                        "两侧口径差异可能过大, 仅供参考不用于下单")
    elif total is not None:
        note = ".tck 不可得: 只有同花顺全量逐笔, 被动侧无法拆出(主动侧缺官方方向)"
    else:
        note = "thsdk 不可得: 只有通达信主动侧, 被动侧(maker)未落盘, 暗盘不完整"

    if active is not None and total is not None:
        coverage = "fusion"
    elif total is not None:
        coverage = "thsdk_only"
    elif active is not None:
        coverage = "tck_only"
    else:
        coverage = "none"

    return {
        "symbol": symbol,
        "active": active,
        "split": split,
        "total": total,
        "passive_est": passive_est,
        "passive_flag": flag,
        "coverage": coverage,
        "note": note,
    }
