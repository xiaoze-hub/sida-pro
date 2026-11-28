"""A 股日线回测：先验约束、已实现交易和未平仓估值不得互相冒充。"""
from dataclasses import asdict

import pytest

from src.core.backtest import service
from src.core.backtest.cost_model import CostModel
from src.core.backtest.data_adapter import PriceBar
from src.core.backtest.engine import Backtester, Signal
from src.core.backtest.portfolio import PortfolioBacktester, PortfolioConfig


D0, D1, D2, D3 = "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"
A, B, C = "600000", "600001", "600002"


def bar(day: str, price: float = 10, *, high: float | None = None, volume: float = 100_000) -> PriceBar:
    return PriceBar(date=day, open=price, high=high or price, low=price, close=price, volume=volume)


@pytest.mark.parametrize("portfolio", [False, True])
@pytest.mark.parametrize(
    "end_bars,close,reason",
    [
        ([], 10, "t_plus_one"),
        ([bar(D2, 9)], 9, "untradeable"),
        ([bar(D2, 9.8, volume=0)], 9.8, "untradeable"),
    ],
)
def test_unsellable_sample_end_is_open_and_valued_net_of_buy_cost(portfolio, end_bars, close, reason):
    initial = 10_000
    cost = CostModel()
    cap = 1_100
    runner = (PortfolioBacktester(PortfolioConfig(initial_capital=initial, position_pct=cap / initial), cost)
              if portfolio else Backtester(cost, initial_capital=initial, cash_per_trade=cap))
    sig = Signal(A, "CN", D0, holding_days=1, stop_loss=9.5, target_price=9)
    bars = [bar(D0), bar(D1), *end_bars]
    outcome = runner.run([sig], {A: bars})
    assert outcome.trades == []
    assert outcome.skipped == 0
    position, = outcome.open_positions
    assert (position.symbol, position.entry_date, position.quantity) == (A, D1, 100)
    assert (position.mark_date, position.mark_price, position.exit_blocked_reason) == (bars[-1].date, close, reason)
    buy = cost.fill("buy", 10, 100)
    ending = initial + buy.cash_delta + 100 * close
    assert outcome.equity_curve[-1] == pytest.approx(ending)
    assert outcome.equity_dates[0] == '' and outcome.equity_curve[0] == initial
    assert outcome.equity_dates[-1] == bars[-1].date
    assert outcome.metrics["total_pnl"] == 0
    assert outcome.metrics["trades"] == 0
    assert outcome.metrics["total_return"] == pytest.approx(ending / initial - 1, abs=5e-7)
    assert position.unrealized_pnl == pytest.approx(ending - initial)


@pytest.mark.parametrize("limited_by", ["cash", "slot"])
def test_intraday_exit_does_not_finance_or_make_room_for_open_buys(limited_by):
    cash_only = limited_by == "cash"
    bt = PortfolioBacktester(PortfolioConfig(initial_capital=1_100, position_pct=1, max_positions=2)
                             if cash_only else PortfolioConfig(initial_capital=5_000, position_pct=0.25, max_positions=1))
    a = Signal(A, "CN", D0, target_price=10.5)
    b = Signal(B, "CN", D1)
    c = Signal(C, "CN", D2)
    outcome = bt.run([a, b, c], {A: [bar(D0), bar(D1), bar(D2, high=10.8)],
                               B: [bar(D1), bar(D2), bar(D3)], C: [bar(D2), bar(D3)]})
    fill, = outcome.trades
    assert (fill.symbol, fill.exit_date, fill.exit_reason, fill.exit_price) == (A, D2, "target", 10.5)
    position, = outcome.open_positions
    assert (position.symbol, position.entry_date) == (C, D3)
    assert (outcome.skipped_cash, outcome.skipped_slots) == ((1, 0) if cash_only else (0, 1))


def test_open_position_remains_in_portfolio_and_marks_from_last_observed_bar():
    bt = PortfolioBacktester(PortfolioConfig(initial_capital=5_000, position_pct=0.25, max_positions=1))
    a = Signal(A, "CN", D0, stop_loss=9.5)
    b = Signal(B, "CN", D2)
    outcome = bt.run([a, b], {A: [bar(D0), bar(D1), bar(D2, 9)], B: [bar(D2), bar(D3)]})
    assert outcome.trades == []
    assert outcome.skipped_slots == 1
    position, = outcome.open_positions
    assert (position.mark_date, outcome.equity_dates[-1]) == (D2, D2)
    expected = outcome.initial_capital + CostModel().fill("buy", 10, position.quantity).cash_delta + 9 * position.quantity
    assert outcome.equity_curve[-1] == pytest.approx(expected)


def test_api_returns_open_position_separately_with_independent_signal_basis(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api.backtest import get_current_user, router

    # 足够的热身柱；倒数第二根形成金叉，末根开盘建仓。
    prefix = [bar(f"2026-10-{day:02d}") for day in range(1, 29)]
    bars = [*prefix, bar(D0, 10.5), bar(D1, 10.5)]
    monkeypatch.setattr(service, "load_price_history", lambda *_a, **_k: bars)
    app = FastAPI()
    app.include_router(router, prefix="/api/backtest")
    app.dependency_overrides[get_current_user] = object
    expected, = Backtester().run([Signal(A, "CN", D0)], {A: bars}).open_positions
    with TestClient(app) as client:
        response = client.post("/api/backtest/run", json={"symbols": [A]})
        malformed = client.post("/api/backtest/run", json={"symbols": [A], "end_date": "2026-11-27-extra"})
    assert malformed.status_code == 422
    assert response.status_code == 200
    result = response.json()
    assert result["trades"] == []
    assert result["open_positions"] == [asdict(expected)]
    assert result["execution_basis"] == "independent_signal_daily_bar"
    assert result["skipped_signals"] == 0


@pytest.mark.parametrize("portfolio", [False, True])
def test_realized_ledger_uses_same_fill_price_as_end_equity(portfolio):
    runner = PortfolioBacktester() if portfolio else Backtester()
    outcome = runner.run([Signal(A, "CN", D0, holding_days=1)],
                         {A: [bar(D0), bar(D1, 10.000049), bar(D2, 10.500041)]})
    trade, = outcome.trades
    assert outcome.equity_curve[-1] - outcome.initial_capital == pytest.approx(trade.pnl, abs=1e-5)


def test_duplicate_entry_day_does_not_unlock_t_plus_one():
    outcome = Backtester().run([Signal(A, "CN", D0)], {A: [bar(D0), bar(D1), bar(D1)]})
    assert outcome.trades == []
    position, = outcome.open_positions
    assert position.exit_blocked_reason == "t_plus_one"


def test_requested_end_date_excludes_future_fill_and_marks(monkeypatch):
    prefix = [bar(f"2026-10-{day:02d}") for day in range(1, 29)]
    bars = [*prefix, bar(D0, 10.5), bar(D1, 10.5), bar(D2, 10.7, high=12)]
    monkeypatch.setattr(service, "load_price_history", lambda *_a, **_k: bars)
    capped = service.run_backtest(symbols=[A], end_date=D1)
    assert capped["trades"] == []
    position, = capped["open_positions"]
    assert position["mark_date"] == capped["equity_dates"][-1] == D1
    assert capped["initial_capital"] == 1_000_000


@pytest.mark.parametrize("portfolio", [False, True])
@pytest.mark.parametrize("invalid_close", [0.0, None])
def test_invalid_last_close_does_not_pollute_mark_or_equity(portfolio, invalid_close):
    runner = PortfolioBacktester() if portfolio else Backtester()
    bars = [bar(D0), bar(D1), bar(D2, 9.8), PriceBar(D3, 10, 10, 10, invalid_close, 100_000)]
    outcome = runner.run([Signal(A, "CN", D0)], {A: bars})
    assert not outcome.trades
    position, = outcome.open_positions
    assert (position.mark_date, position.mark_price) == (D2, 9.8)
    assert outcome.equity_dates[-1] == D2
    assert outcome.equity_curve[-1] == pytest.approx(outcome.initial_capital + position.unrealized_pnl)


def test_unknown_volume_does_not_raise_or_create_exit():
    from typing import cast

    bars = [bar(D0), bar(D1), bar(D2, 9.8, volume=cast(float, None))]
    outcome = Backtester().run([Signal(A, "CN", D0)], {A: bars})
    assert not outcome.trades
    assert outcome.open_positions[0].exit_blocked_reason == "untradeable"


@pytest.mark.parametrize('portfolio', [False, True])
def test_duplicate_final_day_keeps_legacy_sellable_sample_exit(portfolio):
    runner = PortfolioBacktester() if portfolio else Backtester()
    outcome = runner.run([Signal(A, 'CN', D0)], {A: [bar(D0), bar(D1), bar(D2), bar(D2)]})
    assert not outcome.open_positions
    trade, = outcome.trades
    assert (trade.exit_date, trade.exit_reason, trade.holding_bars) == (D2, 'eod', 1)


def test_explicit_end_does_not_force_trade_on_sellable_final_day(monkeypatch):
    prefix = [bar(f'2026-10-{day:02d}') for day in range(1, 29)]
    bars = [*prefix, bar(D0, 10.5), bar(D1, 10.5), bar(D2, 10.6), bar(D3, 13.0)]
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_k: bars)
    result = service.run_backtest(symbols=[A], end_date=D2)
    assert result['trades'] == []
    position, = result['open_positions']
    assert (position['mark_date'], position['exit_blocked_reason']) == (D2, 'sample_end')
    assert result['pnl_basis']['total_pnl'] == 'realized_net'
    assert result['equity_basis'] == 'initial_capital_then_daily_close'
    assert result['params']['sample_end_policy'] == 'mark_open_without_forced_exit'

    expired = service.run_backtest(symbols=[A], end_date=D2, holding_days=1)
    assert expired['trades'][0]['exit_reason'] == 'expire'
    assert not expired['open_positions']


def test_history_ends_before_requested_boundary(monkeypatch):
    prefix = [bar(f'2026-10-{day:02d}') for day in range(1, 29)]
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_k: [*prefix, bar(D0, 10.5), bar(D1, 10.5)])
    result = service.run_backtest(symbols=[A], start_date=D0, end_date=D3)
    position, = result['open_positions']
    assert (position['mark_date'], position['exit_blocked_reason']) == (D1, 'history_exhausted')
    assert result['data_coverage'][A]['bars_in_window'] == 2


def test_dates_and_history_availability_do_not_masquerade_as_no_signals(monkeypatch):
    monkeypatch.setattr(service, 'load_price_history', lambda *_a, **_k: [bar(D0), bar(D1)])
    for bad in ('2026/11/27', '2026-11-01x', '2026-11-31'):
        with pytest.raises(service.BacktestDateError):
            service.run_backtest(symbols=[A], end_date=bad)
    with pytest.raises(service.BacktestDateError):
        service.run_backtest(symbols=[A], start_date=D2, end_date=D0)
    outcome = service.run_backtest(symbols=[A], end_date='2023-10-01')
    assert outcome['data_coverage'][A]['status'] == 'no_bars_in_window'
    assert outcome['skipped_symbols'] == [A]
