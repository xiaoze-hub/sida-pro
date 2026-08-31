# -*- coding: utf-8 -*-
"""OHLC 分摊暗盘(APZJ 对照项)。

## 定位(2026-08-31 Hermes 三封邮件定案, 硬边界)

| 口径 | 算法 | 性质 |
|------|------|------|
| 明盘 | big_order_flow 汇总(30 万阈值) | 表层大单, **会骗人**, 仅参考 |
| 暗盘(APZJ) | **本模块: K线形态 OHLC 分摊** | 同花顺公开的**简化近似**, 用于对齐暗盘榜数字 |
| 暗盘(真实意图) | L2 逐笔委托号 + 拆单识别 | **主线**, 真主力意图, 本模块**不可替代** |

⚠️ 本模块只是**对照项**: 同花顺官方自述"暗盘资金(APZJ)核心假设: K线波动由
大资金引发(50 万以上视为主力资金), 根据 K 线形态将总成交量按规则分摊给买/卖"。
它用 4 个价格 + 成交量**估算**多空力量, 不涉及任何逐笔/委托数据,
**已知失真案例(2026-08-27 神剑股份, 大振幅妖股日): 算出 +2.17 亿 vs 逐笔真实 +939 万,
误差 23 倍**。因此输出必须显式标注 `approximation=True`, 禁止当作下单依据。

## 算法(K线形态分摊)

单根 K 线(O, H, L, C, 成交量 V):

    收盘位置   pos   = (C - L) / (H - L)      # 收盘在当日区间的相对位置 ∈ [0, 1]
    典型价     tp    = (H + L + 2C) / 4        # 当日成交均价近似
    成交金额   amt   = V(股) × tp              # 单位: 元
    买入额     buy   = amt × pos               # 收盘越靠上, 买方越强
    卖出额     sell  = amt × (1 - pos)

特殊处理:
    - H == L(一字板): pos 无意义 → C > O 按全买, C < O 按全卖, C == O 按中性(0.5)
    - 任一字段缺失/非正 → 该根跳过并计数, 不按 0 补齐

多周期(1 日 / 3 日 / 5 日): 对最近 N 根逐日分摊后求和。

单位口径: 成交量 = 股, 金额 = 元(项目硬约束)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

DEFAULT_TYPICAL_PRICE = "hlcc4"  # (H+L+2C)/4


@dataclass
class BarAllocation:
    """单根 K 线分摊结果。金额单位 = 元。"""

    date: str
    buy: float          # 分摊买入额(元)
    sell: float         # 分摊卖出额(元)
    amount: float       # 当日成交金额(元)
    pos: Optional[float]  # 收盘位置 [0,1]; 一字板为 None
    net: float          # buy - sell(元)


def _typical_price(h: float, l: float, c: float, mode: str = DEFAULT_TYPICAL_PRICE) -> float:
    """当日成交均价近似。当前实现 (H+L+2C)/4。"""
    return (h + l + 2.0 * c) / 4.0


def allocate_bar(o: float, h: float, l: float, c: float, volume: float,
                 date: str = "") -> Optional[BarAllocation]:
    """单根 K 线 OHLC 分摊。

    Args:
        o/h/l/c: 开/高/低/收(元)
        volume: 成交量(股)
        date: 日期串(仅透传, 便于结果回溯)

    Returns:
        BarAllocation; 字段非法(非数/负值/量价任一<=0)返回 None(调用方跳过, 不编造)。
    """
    vals = (o, h, l, c, volume)
    if any(not isinstance(v, (int, float)) for v in vals):
        return None
    if o <= 0 or h <= 0 or l <= 0 or c <= 0 or volume <= 0:
        return None
    if h < l:  # 脏数据: 最高 < 最低, 无法分摊
        return None

    tp = _typical_price(h, l, c)
    amount = float(volume) * tp

    if h == l:
        # 一字板: 收盘位置无意义, 用开收关系定多空
        if c > o:
            pos = 1.0
        elif c < o:
            pos = 0.0
        else:
            pos = 0.5
        buy = amount * pos
        sell = amount * (1.0 - pos)
        return BarAllocation(date=date, buy=buy, sell=sell, amount=amount,
                             pos=None, net=buy - sell)

    pos = (c - l) / (h - l)
    buy = amount * pos
    sell = amount * (1.0 - pos)
    return BarAllocation(date=date, buy=buy, sell=sell, amount=amount,
                         pos=pos, net=buy - sell)


def ohlc_dark_net(
    bars: Sequence[dict],
    days: Optional[int] = None,
) -> dict:
    """对最近 N 根 K 线做 OHLC 分摊, 返回 APZJ 对照净额(元)。

    Args:
        bars: 按日期升序的日K, 每根需含 open/high/low/close/volume(股)。
        days: 取最近 N 根; None = 全部。

    Returns:
        {
          "dark_net": 元,          # Σ(买-卖), APZJ 对照值
          "buy_total": 元,
          "sell_total": 元,
          "bars_used": int,
          "bars_skipped": int,
          "days": int | None,
          "approximation": True,   # 硬标记: 近似对照项, 非真实意图
          "per_bar": [BarAllocation...],
        }
        bars 为空 → 显式返回 "dark_net": None 并标 "无数据", 不返回 0 冒充。
    """
    if not bars:
        return {
            "dark_net": None,
            "buy_total": 0.0,
            "sell_total": 0.0,
            "bars_used": 0,
            "bars_skipped": 0,
            "days": days,
            "approximation": True,
            "per_bar": [],
            "note": "无数据",
        }

    window = list(bars)[-days:] if days else list(bars)
    allocs: list[BarAllocation] = []
    skipped = 0
    for b in window:
        a = allocate_bar(
            o=b.get("open"), h=b.get("high"), l=b.get("low"),
            c=b.get("close"), volume=b.get("volume"),
            date=str(b.get("date", "")),
        )
        if a is None:
            skipped += 1
            continue
        allocs.append(a)

    if not allocs:
        return {
            "dark_net": None,
            "buy_total": 0.0,
            "sell_total": 0.0,
            "bars_used": 0,
            "bars_skipped": skipped,
            "days": days,
            "approximation": True,
            "per_bar": [],
            "note": "无数据(全部字段非法)",
        }

    buy_total = sum(a.buy for a in allocs)
    sell_total = sum(a.sell for a in allocs)
    return {
        "dark_net": round(buy_total - sell_total, 2),
        "buy_total": round(buy_total, 2),
        "sell_total": round(sell_total, 2),
        "bars_used": len(allocs),
        "bars_skipped": skipped,
        "days": days,
        "approximation": True,
        "per_bar": allocs,
    }
