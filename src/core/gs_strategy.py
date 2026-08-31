# -*- coding: utf-8 -*-
"""GS 信号(阶段1 五件套 ①)。

算法内核严格复用 `decision_pioneer.compute_gs_signal`(BB0/A0 交叉),
本模块只补充官方语义层, 不改公式:

    BB0 慢线 = (MA3 + MA7 + MA13 + MA27) / 4
    A0  快线 = (H + L + 2O + 6C) / 10
    A0 上穿 BB0 → G(趋势有望启动); 下穿 → S(趋势暂或结束)

## 官方语义(决策先锋 8 问 8 答)
- **非函数指标**: 信号出现后**次日不消失**, 处于 G 区间直到出现 S
- **收盘定死才出**: 只报**已确认**的历史交叉;
  最后一根 K 线的疑似交叉(A0 用的是盘中价)标 `pending`, **不作为 G/S 信号输出**,
  防止 GS 日线均线交叉的右侧滞后坑(与前端 InteractiveKline 的实心/空心圆同口径)

## 数据
日K(开/高/低/收/量), 至少 28 根(BB0 需 MA27)。
缺失显式 None / "无数据", 不编造。
"""

from __future__ import annotations

from typing import Optional, Sequence

from src.core.decision_pioneer import compute_gs_signal

# 区域语义
ZONE_G = "G区"
ZONE_S = "S区"


def _is_cross_on_last_bar(gs: dict, n_bars: int) -> bool:
    """最近一次交叉是否就发生在最后一根(= 盘中/当日, 待确认)。"""
    idx = gs.get("last_cross_idx")
    return isinstance(idx, int) and idx == n_bars - 1


def eval_gs(bars: Sequence[dict]) -> dict:
    """计算 GS 信号(官方语义)。

    Args:
        bars: 按日期升序的日K, 每根含 open/high/low/close。

    Returns:
        {
          "zone": "G区" / "S区" / None,
          "signal": "G" / "S" / None,     # 已确认的最近一次交叉方向
          "signal_confirmed": bool,       # 该交叉是否收盘定死(非末根)
          "pending": bool,                # 末根是否存在疑似交叉(待确认)
          "bb0": float | None,
          "a0": float | None,
          "new_g_today": bool,            # 今日新出已确认 G(共振状态机用)
          "new_s_today": bool,
        }
        数据不足 → 全 None 字段 + note "无数据"。
    """
    n = len(bars or [])
    if n < 28:
        return {
            "zone": None, "signal": None, "signal_confirmed": False,
            "pending": False, "bb0": None, "a0": None,
            "new_g_today": False, "new_s_today": False,
            "note": "无数据(日K < 28 根, BB0 需 MA27)",
        }

    gs = compute_gs_signal(list(bars))
    if not gs:
        return {
            "zone": None, "signal": None, "signal_confirmed": False,
            "pending": False, "bb0": None, "a0": None,
            "new_g_today": False, "new_s_today": False,
            "note": "无数据",
        }

    cross_on_last = _is_cross_on_last_bar(gs, n)
    signal = gs.get("signal")
    state = gs.get("state")
    zone = ZONE_G if state == "G区" else (ZONE_S if state == "S区" else None)

    # 已确认 = 交叉不在最后一根(最后一根的 C 是盘中价, 收盘前仍可能回穿)
    confirmed = bool(signal) and not cross_on_last
    return {
        "zone": zone,
        "signal": signal,
        "signal_confirmed": confirmed,
        "pending": bool(signal) and cross_on_last,
        "bb0": gs.get("bb0"),
        "a0": gs.get("a0"),
        "new_g_today": bool(signal == "G" and confirmed),
        "new_s_today": bool(signal == "S" and confirmed),
    }


def compute_gs_signals(bars: Sequence[dict]) -> list[dict]:
    """全量 GS 交叉信号序列(summary API / 前端 L2 图层用)。

    公式与 `compute_gs_signal` 完全一致(BB0/A0 交叉, 前后端等价已实测验证),
    区别是本函数返回**每一根**的交叉, 而非只返回最近一次。

    Returns:
        [{date, side, confirmed, price}, ...] 按时间升序。
        side: "G" / "S"
        confirmed: True = 收盘定死(历史 bar); False = 末根疑似(盘中价, 待确认)
        price: 该根收盘价(元)
        数据不足(任一根 BB0 不可算)或空输入 → []。
    """
    out: list[dict] = []
    n = len(bars or [])
    if n < 28:
        return out

    from src.core.decision_pioneer import _bar_close_open_high_low, _sma

    closes: list[float] = []
    a0: list[float] = []
    bb0: list[Optional[float]] = []
    dates: list[str] = []
    for b in bars:
        c, o, h, l = _bar_close_open_high_low(b)
        closes.append(c)
        a0.append((h + l + 2 * o + 6 * c) / 10)
        bb0s = []
        for p in (3, 7, 13, 27):
            m = _sma(closes, p)
            if m is None:
                bb0s = None  # type: ignore[assignment]
                break
            bb0s.append(m)
        bb0.append(sum(bb0s) / 4 if bb0s else None)  # type: ignore[arg-type]
        dates.append(str(getattr(b, "date", None) or (b.get("date") if isinstance(b, dict) else "")))

    for i in range(1, n):
        if bb0[i] is None or bb0[i - 1] is None:
            continue
        prev_diff = a0[i - 1] - bb0[i - 1]
        cur_diff = a0[i] - bb0[i]
        side: Optional[str] = None
        if prev_diff <= 0 < cur_diff:
            side = "G"
        elif prev_diff >= 0 > cur_diff:
            side = "S"
        if side is None:
            continue
        out.append({
            "date": dates[i],
            "side": side,
            "confirmed": i < n - 1,   # 末根 = 待确认
            "price": closes[i],
        })
    return out


def trend_label(gs_eval: dict) -> str:
    """趋势标签(共振状态机输入): G信号 / G区间 / S信号 / S区间 / 无数据。

    - G信号: 今日新出已确认 G(或当前 G区 且最近信号为已确认 G)
    - G区间: 处于 G区(非新出)
    - S信号: 今日新出已确认 S
    - S区间: 处于 S区(非新出)
    """
    if not gs_eval or gs_eval.get("zone") is None:
        return "无数据"
    zone = gs_eval["zone"]
    sig = gs_eval.get("signal")
    confirmed = gs_eval.get("signal_confirmed")
    if zone == ZONE_G:
        # 处于 G区: 若最近一次交叉是已确认 G → G信号, 否则 G区间
        return "G信号" if (sig == "G" and confirmed) else "G区间"
    # S区
    return "S信号" if (sig == "S" and confirmed) else "S区间"
