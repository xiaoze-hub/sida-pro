"""B2.1: 组合级撮合 —— 现金 / 并发 / 容量约束下的共享账户回测。"""

from __future__ import annotations

from src.core.backtest.data_adapter import PriceBar
from src.core.backtest.engine import Backtester, Signal
from src.core.backtest.portfolio import PortfolioBacktester, PortfolioConfig


def _bars(n: int = 20, price: float = 10.0, volume: float = 1e6, start_day: int = 1) -> list[PriceBar]:
    return [
        PriceBar(date=f"2026-06-{start_day + i:02d}", open=price, high=price * 1.01,
                 low=price * 0.99, close=price, volume=volume)
        for i in range(n)
    ]


def _sig(symbol: str, day: str = "2026-06-01", **kw) -> Signal:
    return Signal(symbol, "CN", day, stop_loss=kw.get("stop", 5.0),
                  target_price=kw.get("target", 99.0), holding_days=kw.get("hold", 3))


def test_cash_constraint_skips_when_unaffordable():
    """全部资金买第一笔后, 第二笔买不起一手 → skipped_cash=1。"""
    bt = PortfolioBacktester(PortfolioConfig(
        initial_capital=10_000.0, position_pct=1.0, max_positions=10,
    ))
    bars = {("600000", "CN"): _bars(), ("600001", "CN"): _bars()}
    res = bt.run([_sig("600000"), _sig("600001")], bars)
    assert len(res.trades) == 1
    assert res.skipped_cash == 1 and res.skipped_slots == 0


def test_max_positions_cap():
    """并发上限 2 → 同日 4 个信号只成交 2 笔。"""
    bt = PortfolioBacktester(PortfolioConfig(
        initial_capital=1_000_000.0, position_pct=0.1, max_positions=2,
    ))
    bars = {(f"60000{i}", "CN"): _bars() for i in range(4)}
    sigs = [_sig(f"60000{i}") for i in range(4)]
    res = bt.run(sigs, bars)
    assert len(res.trades) == 2
    assert res.skipped_slots == 2


def test_capacity_caps_quantity():
    """成交量 1000 × 5% = 50 股 → 不足一手, 该信号在规划阶段即被丢弃。"""
    bt = PortfolioBacktester(PortfolioConfig(initial_capital=1_000_000.0, position_pct=0.1))
    bars = {("600000", "CN"): _bars(volume=1000)}
    res = bt.run([_sig("600000")], bars)
    assert res.trades == []


def test_equity_consistent_with_realized_pnl():
    """约束下现金不为负, 且终点净值 = 期初 + 已实现盈亏。"""
    bt = PortfolioBacktester(PortfolioConfig(
        initial_capital=200_000.0, position_pct=0.3, max_positions=3,
    ))
    bars = {(f"60000{i}", "CN"): _bars(price=10.0 + i) for i in range(5)}
    sigs = [_sig(f"60000{i}") for i in range(5)]
    res = bt.run(sigs, bars)
    assert res.trades, "至少应成交若干笔"
    assert all(t.quantity > 0 for t in res.trades)
    realized = sum(t.pnl for t in res.trades)
    assert abs(res.equity_curve[-1] - (res.initial_capital + realized)) < 1e-6
    assert min(res.equity_curve) > 0


def test_matches_single_engine_when_unconstrained():
    """资金充裕且不触上限时, 组合引擎与单笔内核的成交应一致(共用 simulate_exit)。"""
    bars = {("600000", "CN"): _bars(price=10.0)}
    sig = _sig("600000")
    single = Backtester(initial_capital=1_000_000.0, cash_per_trade=100_000.0).run([sig], bars)
    port = PortfolioBacktester(PortfolioConfig(
        initial_capital=1_000_000.0, position_pct=0.1, max_positions=10,
    )).run([sig], bars)
    assert len(single.trades) == len(port.trades) == 1
    s, p = single.trades[0], port.trades[0]
    assert (s.entry_date, s.exit_date, s.exit_reason, s.quantity) == (
        p.entry_date, p.exit_date, p.exit_reason, p.quantity)
    assert abs(s.pnl - p.pnl) < 1e-6
