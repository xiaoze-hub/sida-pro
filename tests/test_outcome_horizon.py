"""B0.3: 后验窗口按交易日计, 缺失目标日不再静默回退(KI-033)。"""

from datetime import date, timedelta

from src.core.backtest.data_adapter import PriceBar
from src.core.backtest.engine import Signal, horizon_return
from src.core.trading_calendar import add_trading_days, is_trading_day


def _bar(date_str, close):
    return PriceBar(date=date_str, open=close, high=close, low=close, close=close, volume=1e6)


def test_horizon_target_is_trading_day_count():
    """target = 起算日起第 N 个交易日(不是自然日)。"""
    snap = date(2026, 1, 1)
    target = add_trading_days(snap, 5)
    assert is_trading_day(target)
    count, cur = 0, snap
    while cur < target:
        cur = cur + timedelta(days=1)
        if is_trading_day(cur):
            count += 1
    assert count == 5


def test_horizon_skips_non_trading_weekdays():
    """窗口内必须跳过法定节假日(自然日口径不会跳)。"""
    snap = date(2026, 9, 30)
    target = add_trading_days(snap, 3)
    skipped = 0
    cur = snap
    while cur < target:
        cur = cur + timedelta(days=1)
        if cur.weekday() < 5 and not is_trading_day(cur):
            skipped += 1
    assert skipped >= 1, "跨国庆窗口应跳过工作日假期"


def test_horizon_return_strict_exact_day():
    """目标日必须精确匹配: 缺失 → None, 不回退更早收盘。"""
    snap = "2026-09-30"
    target = add_trading_days(date(2026, 9, 30), 2).isoformat()
    bars = [_bar("2026-09-30", 10.0), _bar(target, 12.0)]
    sig = Signal("X", "CN", snap, entry_price=10.0)
    r = horizon_return(sig, bars, 2)
    assert r is not None and abs(r - 20.0) < 1e-9
    assert horizon_return(sig, bars[:1], 2) is None


def test_pick_close_strict_rejects_earlier_close():
    """strategy_engine 的 strict 模式: 只有当日收盘才算, 否则 None。"""
    from src.core.strategy_engine import _pick_close_on_or_before

    class K:
        def __init__(self, d, c):
            self.date = d
            self.close = c

    klines = [K("2026-01-02", 11.0), K("2026-01-05", 12.0)]
    assert _pick_close_on_or_before(klines, date(2026, 1, 5), strict=True) == 12.0
    assert _pick_close_on_or_before(klines, date(2026, 1, 4), strict=True) is None
    # 非 strict(基准价用): 允许回退到更早收盘
    assert _pick_close_on_or_before(klines, date(2026, 1, 4)) == 11.0
