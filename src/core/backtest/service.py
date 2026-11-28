"""回测服务(B2.4/KI-039): 把内核接成"给标的+区间就出结果"的可调用入口。

策略口径(默认, 可调参):
- 信号: MA5 上穿 MA20(金叉), **仅用截至当日的收盘价**, 无未来函数;
- 入场: 信号次日开盘(内核规则); 止损/止盈: 信号日收盘 × (1∓pct);
- 撮合: 涨跌停不可成交 + 成交量参与率上限(内核 B0.2 规则)。
"""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone

from src.core.backtest.data_adapter import (
    MAX_FALLBACK_BARS, HistoryAfterWindow, HistoryOutsideLookback, PriceBar, history_window, load_price_history,
)
from src.core.backtest.engine import Backtester, Signal, expected_trading_boundary, positive_trade_volume, valid_ohlc, valid_trade_price
from src.core.trading_calendar import TradingCalendarError, is_trading_day

MIN_BARS = 30
# Calendar floor that can actually yield MIN_BARS sessions (5/7 weekdays plus holidays).
MIN_HISTORY_DAYS = math.ceil(MIN_BARS * 7 / 5) + 8
MAX_SYMBOLS = 20
# Beyond this a request only widens the query/fetch window; coverage flags explain gaps.
MAX_HISTORY_DAYS = 6000


class BacktestDateError(ValueError):
    """Invalid user-supplied calendar boundary."""


def _check_date(name: str, value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return value
    except (TypeError, ValueError):
        pass
    raise BacktestDateError(f"{name} 必须是有效的 YYYY-MM-DD 日期")


def _missing_sessions(
    market: str, bars: list[PriceBar], calendar: dict[date, bool | None],
) -> tuple[int | None, int | None]:
    """Sessions without bars (gaps or halts), plus days outside available calendars."""
    if market not in {'CN', 'SH', 'SZ', 'BJ'} or not bars:
        return None, None
    observed = {bar.date for bar in bars}
    try:
        first, last = date.fromisoformat(min(observed)), date.fromisoformat(max(observed))
    except ValueError:
        return None, None
    missing = unknown = checked = 0
    for offset in range((last - first).days + 1):
        day = first + timedelta(days=offset)
        if day not in calendar:
            try:
                calendar[day] = is_trading_day(day)
            except TradingCalendarError:
                calendar[day] = None
        if calendar[day] is None:
            unknown += 1
        else:
            checked += 1
            missing += calendar[day] is True and day.isoformat() not in observed
    return (missing if checked else None), unknown


def _golden_cross_signals(
    symbol: str,
    market: str,
    bars: list[PriceBar],
    *,
    start_date: str | None,
    end_date: str | None,
    holding_days: int,
    stop_pct: float,
    target_pct: float,
) -> list[Signal]:
    closes = [b.close for b in bars]
    out: list[Signal] = []
    valid_run = 0
    for i, bar in enumerate(bars):
        # Both current and previous MA20 must be formed from consecutive valid bars.
        valid_run = valid_run + 1 if valid_trade_price(bar.close) else 0
        if valid_run < 21:
            continue
        d = bar.date
        if start_date and d < start_date:
            continue
        if end_date and d > end_date:
            break
        ma5_prev = sum(closes[i - 5:i]) / 5
        ma20_prev = sum(closes[i - 20:i]) / 20
        ma5 = sum(closes[i - 4:i + 1]) / 5
        ma20 = sum(closes[i - 19:i + 1]) / 20
        if ma5_prev <= ma20_prev and ma5 > ma20:
            c = closes[i]
            out.append(
                Signal(
                    symbol=symbol,
                    market=market,
                    signal_date=d,
                    stop_loss=round(c * (1 - stop_pct), 4) if stop_pct > 0 else None,
                    target_price=round(c * (1 + target_pct), 4) if target_pct > 0 else None,
                    holding_days=holding_days,
                )
            )
    return out


def run_backtest(
    *,
    symbols: list[str],
    market: str = "CN",
    start_date: str | None = None,
    end_date: str | None = None,
    holding_days: int = 10,
    stop_pct: float = 0.08,
    target_pct: float = 0.15,
    initial_capital: float = 1_000_000.0,
    cash_per_trade: float = 100_000.0,
    participation_rate: float = 0.05,
    days: int = 800,
) -> dict:
    """截至 end_date（含）回测，不以显式样本边界代替策略退出。"""
    start_date = _check_date('start_date', start_date)
    end_date = _check_date('end_date', end_date)
    if start_date and end_date and start_date > end_date:
        raise BacktestDateError('start_date 不得晚于 end_date')
    # load_qfq_bars cuts relative to now; stretch the lookback so an older
    # requested start is not silently reported as fully covered.
    requested_days = max(int(days), 1)
    sample_start = start_date
    if sample_start is None and end_date:
        # End-only requests are anchored to their own end, not to today's date.
        first_ordinal = max(1, date.fromisoformat(end_date).toordinal() - requested_days)
        sample_start = date.fromordinal(first_ordinal).isoformat()
    history_days = max(requested_days, MIN_HISTORY_DAYS)
    now = datetime.now(timezone.utc)
    if sample_start:
        # Keep the extra history for signal warmup, outside the measured sample.
        history_days = max(history_days, (now.date() - date.fromisoformat(sample_start)).days + 60)

    history_capped = history_days > MAX_HISTORY_DAYS
    history_days = min(history_days, MAX_HISTORY_DAYS)
    mkt = (market or "CN").strip().upper() or "CN"
    first_possible, ceiling = history_window(mkt, history_days, now)
    cutoff = first_possible.isoformat()
    coverage_start = sample_start or cutoff
    expected_first = expected_trading_boundary(mkt, coverage_start)
    expected_last = expected_trading_boundary(mkt, end_date, right=True)
    effective_end = min(end_date, ceiling.isoformat()) if end_date else None
    observable_end = expected_trading_boundary(mkt, effective_end, right=True)
    first_target = expected_first or sample_start
    last_target = expected_last or end_date
    left_edge_capped = history_capped and (sample_start is None or bool(first_target and first_target < cutoff))
    no_market_sessions = bool(expected_first and expected_last and expected_first > expected_last)
    outside_lookback = bool(history_capped and last_target and last_target < cutoff)
    bars_by_symbol: dict = {}
    signals: list[Signal] = []
    skipped_symbols: list[str] = []
    coverage = {}
    calendar: dict[date, bool | None] = {}
    # A repeated code would otherwise open two independent positions and double-count PnL.
    requested = list(dict.fromkeys(code for code in (str(s or "").strip() for s in symbols) if code))
    skipped_symbols.extend(requested[MAX_SYMBOLS:])
    for code in requested[:MAX_SYMBOLS]:
        outside_fallback = None
        after_fallback = None
        fallback_limit_reached = False
        loaded = []
        if not outside_lookback and not no_market_sessions:
            try:
                loaded = load_price_history(code, mkt, days=history_days, as_of=now,
                                            through=date.fromisoformat(effective_end) if effective_end else None)
            except HistoryOutsideLookback as exc:
                outside_fallback = exc.latest_date
            except HistoryAfterWindow as exc:
                after_fallback = exc.first_date
                fallback_limit_reached = exc.limit_reached
        bars = loaded
        if effective_end:
            bars = [bar for bar in bars if bar.date <= effective_end]
        if no_market_sessions:
            status = 'no_market_sessions'
        elif outside_lookback:
            status = 'window_before_history_cutoff'
        elif outside_fallback is not None:
            status = 'history_before_lookback'
        elif after_fallback is not None:
            status = ('fallback_budget_exhausted_before_window' if fallback_limit_reached
                      else 'history_starts_after_window')
        elif not loaded:
            status = 'history_unavailable'
        elif not bars:
            status = 'history_starts_after_window'
        elif sample_start and bars[-1].date < sample_start:
            status = 'history_ends_before_window'
        elif len(bars) < MIN_BARS:
            status = 'insufficient_history'
        else:
            status = 'usable'
        window_last = bars[-1].date if bars and (not sample_start or bars[-1].date >= sample_start) else None
        loaded_first = loaded[0].date if loaded else None
        truncated: bool | None = None
        if left_edge_capped and not no_market_sessions:
            truncated = True
        elif loaded_first and loaded_first <= coverage_start:
            truncated = False
        elif loaded_first and expected_first:
            # This compares to the market calendar; it does not assert a vendor gap.
            truncated = loaded_first > expected_first
        window_bars = [bar for bar in bars if not sample_start or bar.date >= sample_start]
        missing_sessions, unverified_days = _missing_sessions(mkt, window_bars, calendar)
        invalid_prices = sum(not valid_ohlc(bar) for bar in window_bars)
        invalid_warmup = sum(not valid_ohlc(bar) for bar in bars[:len(bars) - len(window_bars)])
        coverage[code] = dict(status=status, loaded_first=loaded_first,
                              loaded_last=loaded[-1].date if loaded else None,
                              window_last=window_last,
                              bars_through_end=len(bars),
                              bars_in_window=len(window_bars),
                              missing_sessions_between_bars=missing_sessions,
                              calendar_unverified_days_between_bars=unverified_days,
                              latest_fallback_bar_before_cutoff=outside_fallback,
                              first_fallback_bar_after_ceiling=after_fallback,
                              oldest_loaded_after_query_cutoff=(loaded_first > cutoff if loaded_first else None),
                              nonpositive_or_unknown_volume_bars=sum(not positive_trade_volume(bar.volume) for bar in window_bars),
                              invalid_ohlc_bars=invalid_prices,
                              invalid_warmup_ohlc_bars=invalid_warmup,
                              expected_first_trading_date=expected_first,
                              expected_last_trading_date=expected_last,
                              observable_end_date=observable_end,
                              window_truncated=truncated,
                              left_edge_capped=left_edge_capped,
                              end_after_last_bar=(window_last < observable_end if window_last and observable_end else None))
        if status != 'usable':
            skipped_symbols.append(code)
            continue
        bars_by_symbol[(code, mkt)] = bars
        produced = _golden_cross_signals(
            code, mkt, bars,
            start_date=sample_start, end_date=end_date,
            holding_days=holding_days, stop_pct=stop_pct, target_pct=target_pct,
        )
        signals.extend(produced)

    bt = Backtester(
        initial_capital=initial_capital,
        cash_per_trade=cash_per_trade,
        participation_rate=participation_rate,
        sample_end=effective_end,
        sample_start=sample_start,
    )
    result = bt.run(signals, bars_by_symbol)
    equity_days = result.equity_dates[1:]  # Initial cash is not a trading day.
    return {
        "params": {
            "market": mkt,
            "start_date": start_date or "",
            "end_date": end_date or "",
            "effective_start_date": sample_start or "",
            "holding_days": holding_days,
            "stop_pct": stop_pct,
            "target_pct": target_pct,
            "initial_capital": initial_capital,
            "cash_per_trade": cash_per_trade,
            "participation_rate": participation_rate,
            "strategy": "ma5_cross_ma20",
            "sample_end_policy": "mark_open_without_forced_exit" if end_date else "legacy_liquidate_if_sellable",
            "history_days": history_days,
            "history_capped": history_capped,
            "history_cutoff_date": cutoff,
            "history_ceiling_date": ceiling.isoformat(),
            "coverage_start_date": coverage_start,
            "fallback_bar_limit": min(history_days, MAX_FALLBACK_BARS),
        },
        "universe": sorted(bars_by_symbol.keys(), key=lambda k: k[0]),
        "skipped_symbols": skipped_symbols,
        "data_coverage": coverage,
        "signals": len(signals),
        "skipped_signals": result.skipped,
        "execution_basis": "independent_signal_daily_bar",
        "price_adjustment_basis": "qfq_visible_at_query_time_not_point_in_time",
        "portfolio_constraint": "none",
        "session_gap_basis": "calendar_sessions_without_a_bar_includes_possible_halts",
        "equity_basis": "initial_capital_then_daily_close",
        "equity_end_policy": "observed_cash_days_to_sample_end_if_flat_otherwise_last_fill_or_mark",
        # Requested bounds vs the window actually measured (open positions cap the right edge).
        "sample_window": {"effective_start": result.sample_start, "requested_end": end_date,
                          "effective_end": result.sample_end,
                          "measured_days": len(equity_days)},
        "equity_window": {"first_date": equity_days[0] if equity_days else None,
                          "last_date": equity_days[-1] if equity_days else None,
                          "observed_days": len(equity_days)},
        "pnl_basis": {"total_pnl": "realized_net", "unrealized_pnl": "mark_less_buy_outflow_excludes_exit_cost"},
        # Curve-based metrics include open positions; trade statistics never do.
        "metrics_basis": {"total_return_max_drawdown_sharpe": "mark_to_market_equity",
                          "trades_win_rate_profit_factor_total_pnl": "realized_closed_only",
                          "annualization_window": "equity_dates[1:]"},

        "initial_capital": result.initial_capital,
        # Fills stay exact so prices, fees and pnl reconcile with the ledger.
        "trades": [asdict(t) for t in result.trades],
        "open_positions": [asdict(p) for p in result.open_positions],
        "equity_curve": result.equity_curve,
        "equity_dates": result.equity_dates,
        "metrics": result.metrics,
    }
