"""B4.3/KI-037: 统一指标库与既有 kline_collector 实现逐值对齐(行为零变更)。"""

from __future__ import annotations

import pytest

from src.collectors import kline_collector as kc
from src.collectors.kline_collector import KlineData
from src.core import indicators as ind


def _bars(n: int = 40) -> list[KlineData]:
    """确定性 K 线序列(带波动, 便于区分口径差异)。"""
    out = []
    price = 10.0
    for i in range(n):
        price = price * (1 + ((i % 7) - 3) * 0.004)
        high = price * 1.012
        low = price * 0.988
        out.append(KlineData(date=f"2026-08-{i + 1:02d}", open=price, close=price,
                             high=high, low=low, volume=1000 + i))
    return out


def _closes(bars: list[KlineData]) -> list[float]:
    return [b.close for b in bars]


def test_ma_parity_and_hand_value():
    closes = _closes(_bars())
    assert kc._calculate_ma(closes, 5) == ind.sma(closes, 5)
    assert ind.sma([1, 2, 3, 4, 5], 5) == 3.0
    assert ind.sma([1, 2], 5) is None


def test_ema_parity_and_seeding():
    closes = _closes(_bars())
    assert kc._ema(closes, 12) == ind.ema_series(closes, 12)
    # 首值播种: 序列第一个元素就是 data[0]
    assert ind.ema_series([10.0, 20.0], 3)[0] == 10.0


def test_atr_parity_and_simple_mean_semantics():
    bars = _bars()
    assert kc._calculate_atr(bars, 14) == ind.atr([(b.high, b.low, b.close) for b in bars], 14)
    # 口径差异: 简单均值 vs Wilder 在波动序列上不相等(留档, 防误改)
    tr_bars = [(10.0, 9.0, 9.5), (11.0, 9.5, 10.5), (12.0, 10.0, 11.0),
               (13.0, 11.0, 12.0), (14.0, 12.0, 13.0), (15.0, 13.0, 14.0)]
    assert ind.atr(tr_bars, 3) != ind.atr_wilder(tr_bars, 3)


def test_macd_parity():
    closes = _closes(_bars())
    assert kc._calculate_macd(closes) == ind.macd(closes)
    assert kc._calculate_macd(closes[:20]) is None  # 不足 slow+signal


def test_rsi_parity_and_cutler_semantics():
    closes = _closes(_bars())
    assert kc._calculate_rsi(closes, 14) == ind.rsi(closes, 14)
    up = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert ind.rsi(up, 5) == 100.0            # 全涨 → 100
    assert ind.rsi(up, 5) != ind.rsi_wilder(up, 5) or ind.rsi_wilder(up, 5) == 100.0


def test_kdj_parity_and_seed():
    bars = _bars()
    assert kc._calculate_kdj(bars) == ind.kdj([(b.high, b.low, b.close) for b in bars])
    k, d, j = ind.kdj([(b.high, b.low, b.close) for b in bars], 9)
    assert len(k) == len(bars) - 8
    assert abs(j[0] - (3 * k[0] - 2 * d[0])) < 1e-12


def test_boll_parity_and_population_std():
    closes = _closes(_bars())
    assert kc._calculate_boll(closes, 20) == ind.boll(closes, 20)
    # 总体标准差(÷period)而非样本标准差(÷(period-1))
    upper, mid, lower, width = ind.boll([1.0, 2.0, 3.0, 4.0, 5.0], 5, 1)
    assert mid == 3.0
    assert upper == pytest.approx(3.0 + (2.0 ** 0.5))   # std=sqrt(2), 总体口径
    assert lower == pytest.approx(3.0 - (2.0 ** 0.5))


def test_all_indicators_fail_soft_on_empty():
    assert ind.sma([], 5) is None
    assert ind.ema_series([], 5) == []
    assert ind.atr([], 14) is None
    assert ind.macd([], 12, 26, 9) is None
    assert ind.rsi([], 14) is None
    assert ind.kdj([], 9) is None
    assert ind.boll([], 20) is None
