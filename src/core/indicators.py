"""统一技术指标库(B4.3/KI-037)。

**口径与既有实现逐字对齐**(行为零变更, 由 `tests/test_indicators_parity.py` 锁定):
- MA / BOLL: 简单均值 / 总体标准差(÷period)
- EMA: 以 `data[0]` 播种(非 SMA 播种), multiplier = 2/(period+1)
- MACD: DIF = EMA(fast) − EMA(slow); DEA = EMA(DIF, signal); HIST = (DIF−DEA)×2
- ATR: 最近 period 个 TR 的**简单均值**(非 Wilder; `atr_wilder` 供研究对照)
- RSI: Cutler 简单均值(非 Wilder; `rsi_wilder` 供研究对照)
- KDJ: K/D 以 50 播种, (2/3, 1/3) 平滑, J = 3K − 2D

数据不足/异常一律返回 None(或空列表), 不抛异常(fail-soft), 与既有调用方一致。
"""

from __future__ import annotations

Bars = list[tuple[float, float, float]]  # [(high, low, close), ...]


def sma(values: list[float], period: int) -> float | None:
    """简单移动平均(最近 period 个)。"""
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: list[float], period: int) -> list[float]:
    """EMA 序列: 首值播种, 递推 (price - prev) * k + prev, k = 2/(period+1)。"""
    if not values or period <= 0:
        return []
    out = [values[0]]
    k = 2 / (period + 1)
    for price in values[1:]:
        out.append((price - out[-1]) * k + out[-1])
    return out


def true_ranges(bars: Bars) -> list[float]:
    """TR = max(high-low, |high-prevClose|, |low-prevClose|)。"""
    out: list[float] = []
    for i in range(1, len(bars)):
        high, low, _ = bars[i]
        prev_close = bars[i - 1][2]
        out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return out


def atr(bars: Bars, period: int = 14) -> float | None:
    """ATR(简单均值口径, 与历史实现一致)。需 period+1 根。"""
    if period <= 0 or len(bars) < period + 1:
        return None
    trs = true_ranges(bars)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def atr_wilder(bars: Bars, period: int = 14) -> float | None:
    """Wilder ATR(研究对照用, 不改变线上口径)。"""
    if period <= 0 or len(bars) < period + 1:
        return None
    trs = true_ranges(bars)
    if len(trs) < period:
        return None
    prev = sum(trs[:period]) / period
    for tr in trs[period:]:
        prev = (prev * (period - 1) + tr) / period
    return prev


def macd(
    closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[list[float], list[float], list[float]] | None:
    """MACD 全序列: (DIF, DEA, HIST)。需 slow+signal 根。"""
    if len(closes) < slow + signal:
        return None
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = ema_series(dif, signal)
    hist = [(d - e) * 2 for d, e in zip(dif, dea)]
    return dif, dea, hist


def rsi(closes: list[float], period: int) -> float | None:
    """RSI(Cutler 简单均值口径, 与历史实现一致)。"""
    if period <= 0 or len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        if change > 0:
            gains.append(change)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(change))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def rsi_wilder(closes: list[float], period: int = 14) -> float | None:
    """RSI(Wilder 平滑, 研究对照用)。"""
    if period <= 0 or len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def kdj(
    bars: Bars, n: int = 9, m1: int = 3, m2: int = 3
) -> tuple[list[float], list[float], list[float]] | None:
    """KDJ 全序列: (K, D, J)。需 n 根。"""
    if n <= 0 or len(bars) < n:
        return None
    k_values: list[float] = []
    d_values: list[float] = []
    j_values: list[float] = []
    for i in range(n - 1, len(bars)):
        window = bars[i - n + 1 : i + 1]
        highest = max(b[0] for b in window)
        lowest = min(b[1] for b in window)
        close = bars[i][2]
        rsv = 50.0 if highest == lowest else (close - lowest) / (highest - lowest) * 100
        if not k_values:
            k, d = 50.0, 50.0
        else:
            k = (2 / 3) * k_values[-1] + (1 / 3) * rsv
            d = (2 / 3) * d_values[-1] + (1 / 3) * k
        k_values.append(k)
        d_values.append(d)
        j_values.append(3 * k - 2 * d)
    return k_values, d_values, j_values


def boll(
    closes: list[float], period: int = 20, num_std: int = 2
) -> tuple[float, float, float, float] | None:
    """布林带: (上轨, 中轨, 下轨, 带宽%)。总体标准差口径(÷period)。"""
    if period <= 0 or len(closes) < period:
        return None
    recent = closes[-period:]
    mid = sum(recent) / period
    variance = sum((x - mid) ** 2 for x in recent) / period
    std = variance**0.5
    upper = mid + num_std * std
    lower = mid - num_std * std
    width = (upper - lower) / mid * 100 if mid > 0 else 0
    return upper, mid, lower, width
