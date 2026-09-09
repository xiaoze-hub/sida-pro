"""B0.2: 涨跌停不可成交 + 成交量参与率上限。

一字涨停买不进(顺延/作废)、一字跌停卖不出(止损/到期顺延)、
单笔成交量 ≤ 当日成交量 × participation_rate。
"""

from src.core.backtest.data_adapter import PriceBar
from src.core.backtest.engine import Backtester, Signal


def _bar(date, o, h, low, c, v=1e6):
    return PriceBar(date=date, open=o, high=h, low=low, close=c, volume=v)


def test_limit_up_sealed_entry_deferred_to_next_fillable_day():
    """一字涨停当日买不进 → 顺延到下一可成交日。"""
    bars = [
        _bar("2026-01-01", 10, 10, 10, 10),
        _bar("2026-01-02", 11.0, 11.0, 11.0, 11.0),   # 昨收 10 → 涨停 11.00, 一字封死
        _bar("2026-01-05", 11.5, 12, 11.4, 11.8),
        _bar("2026-01-06", 12, 12.5, 11.8, 12),
    ]
    sig = Signal("600000", "CN", "2026-01-01", stop_loss=5, target_price=99, holding_days=5)
    t = Backtester().run_single(sig, bars)
    assert t is not None
    assert t.entry_date == "2026-01-05"
    assert t.entry_price == 11.5


def test_all_sealed_limit_up_skips_signal():
    """全程一字板(每天 +10%)→ 该信号作废并计入 skipped。"""
    bars = [
        _bar("2026-01-01", 10, 10, 10, 10),
        _bar("2026-01-02", 11.0, 11.0, 11.0, 11.0),
        _bar("2026-01-05", 12.1, 12.1, 12.1, 12.1),
        _bar("2026-01-06", 13.31, 13.31, 13.31, 13.31),
    ]
    sig = Signal("600000", "CN", "2026-01-01", stop_loss=5, target_price=99, holding_days=5)
    assert Backtester().run_single(sig, bars) is None
    res = Backtester().run([sig], {("600000", "CN"): bars})
    assert res.skipped == 1 and not res.trades


def test_limit_down_blocks_stop_loss_fill():
    """一字跌停卖不出 → 止损顺延到下一可成交日, 按当日开盘成交。"""
    bars = [
        _bar("2026-01-01", 10, 10, 10, 10),
        _bar("2026-01-02", 10, 10.2, 9.9, 10),        # 入场 10
        _bar("2026-01-05", 9.0, 9.0, 9.0, 9.0),       # 昨收 10 → 跌停 9.00, 一字封死
        _bar("2026-01-06", 8.8, 9.2, 8.5, 9.0),       # 可成交, 开盘 8.8 ≤ 止损 9.5
    ]
    sig = Signal("600000", "CN", "2026-01-01", stop_loss=9.5, target_price=99, holding_days=10)
    t = Backtester().run_single(sig, bars)
    assert t is not None
    assert t.exit_reason == "stop_loss"
    assert t.exit_date == "2026-01-06"
    assert t.exit_price == 8.8


def test_volume_cap_limits_quantity():
    """单笔成交量受当日成交量 × 参与率(默认 5%)约束, 向下取整到一手。"""
    bars = [
        _bar("2026-01-01", 10, 10, 10, 10, v=1e6),
        _bar("2026-01-02", 10, 10, 10, 10, v=10_000),  # 10000 × 5% = 500 股
        _bar("2026-01-05", 10, 10, 10, 10),
    ]
    sig = Signal("600000", "CN", "2026-01-01", stop_loss=5, target_price=99, holding_days=3)
    t = Backtester().run_single(sig, bars)
    assert t is not None and t.quantity == 500


def test_volume_too_thin_skips_entry():
    """成交量过小(不足一手) → 顺延无果, 该信号作废。"""
    bars = [
        _bar("2026-01-01", 10, 10, 10, 10, v=1e6),
        _bar("2026-01-02", 10, 10, 10, 10, v=100),    # 100 × 5% = 5 股 → 0 手
    ]
    sig = Signal("600000", "CN", "2026-01-01", stop_loss=5, target_price=99, holding_days=3)
    assert Backtester().run_single(sig, bars) is None


def test_st_limit_ratio_is_five_percent():
    """ST 股涨跌停幅度 5%: 11.0 已超 5%(10→10.50), 不是涨停 → 可买入。"""
    bars = [
        _bar("2026-01-01", 10, 10, 10, 10),
        _bar("2026-01-02", 10.5, 10.5, 10.5, 10.5),   # ST 一字涨停(10.50)
        _bar("2026-01-05", 10.6, 10.8, 10.5, 10.7),
    ]
    # 非 ST 判定: 10→11.00 才是涨停, 10.50 可成交
    t_nost = Backtester().run_single(
        Signal("600000", "CN", "2026-01-01", stop_loss=5, target_price=99, holding_days=3), bars
    )
    assert t_nost is not None and t_nost.entry_date == "2026-01-02"
    # ST 判定: 10.50 一字封死 → 顺延到 01-05
    t_st = Backtester().run_single(
        Signal("600000", "CN", "2026-01-01", stop_loss=5, target_price=99, holding_days=3, is_st=True),
        bars,
    )
    assert t_st is not None and t_st.entry_date == "2026-01-05"
