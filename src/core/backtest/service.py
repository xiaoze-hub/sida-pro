"""回测服务(B2.4/KI-039): 把内核接成"给标的+区间就出结果"的可调用入口。

策略口径(默认, 可调参):
- 信号: MA5 上穿 MA20(金叉), **仅用截至当日的收盘价**, 无未来函数;
- 入场: 信号次日开盘(内核规则); 止损/止盈: 信号日收盘 × (1∓pct);
- 撮合: 涨跌停不可成交 + 成交量参与率上限(内核 B0.2 规则)。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

from src.core.backtest.data_adapter import load_price_history
from src.core.backtest.engine import Backtester, Signal

MIN_BARS = 30
MAX_SYMBOLS = 20


class BacktestDateError(ValueError):
    """Invalid user-supplied calendar boundary."""


def _check_date(name: str, value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() == value:
            return value
    except ValueError:
        pass
    raise BacktestDateError(f"{name} 必须是有效的 YYYY-MM-DD 日期")


def _golden_cross_signals(
    symbol: str,
    market: str,
    bars,
    *,
    start_date: str | None,
    end_date: str | None,
    holding_days: int,
    stop_pct: float,
    target_pct: float,
) -> list[Signal]:
    closes = [b.close for b in bars]
    out: list[Signal] = []
    for i in range(20, len(bars)):
        d = bars[i].date
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
    history_days = max(int(days), 30)
    earliest = start_date or end_date
    if earliest:
        history_days = max(history_days, (date.today() - date.fromisoformat(earliest)).days + 60)
    mkt = (market or "CN").strip().upper() or "CN"
    bars_by_symbol: dict = {}
    signals: list[Signal] = []
    skipped_symbols: list[str] = []
    coverage = {}
    for sym in list(symbols)[:MAX_SYMBOLS]:
        code = str(sym or "").strip()
        if not code:
            continue
        loaded = load_price_history(code, mkt, days=history_days)
        bars = loaded
        if end_date:
            bars = [bar for bar in bars if bar.date <= end_date]
        if not loaded:
            status = 'history_unavailable'
        elif not bars or (start_date and bars[-1].date < start_date):
            status = 'no_bars_in_window'
        elif len(bars) < MIN_BARS:
            status = 'insufficient_history'
        else:
            status = 'usable'
        coverage[code] = dict(status=status, loaded_first=loaded[0].date if loaded else None,
                              loaded_last=loaded[-1].date if loaded else None,
                              bars_through_end=len(bars),
                              bars_in_window=sum(not start_date or bar.date >= start_date for bar in bars),
                              window_truncated=bool(start_date and loaded and loaded[0].date > start_date),
                              # A non-trading-day boundary also produces this; it is not proof of missing data.
                              end_after_last_bar=bool(end_date and loaded and loaded[-1].date < end_date))
        if status != 'usable':
            skipped_symbols.append(code)
            continue
        bars_by_symbol[(code, mkt)] = bars
        signals.extend(
            _golden_cross_signals(
                code, mkt, bars,
                start_date=start_date, end_date=end_date,
                holding_days=holding_days, stop_pct=stop_pct, target_pct=target_pct,
            )
        )

    bt = Backtester(
        initial_capital=initial_capital,
        cash_per_trade=cash_per_trade,
        participation_rate=participation_rate,
        sample_end=end_date,
    )
    result = bt.run(signals, bars_by_symbol)
    return {
        "params": {
            "market": mkt,
            "start_date": start_date or "",
            "end_date": end_date or "",
            "holding_days": holding_days,
            "stop_pct": stop_pct,
            "target_pct": target_pct,
            "initial_capital": initial_capital,
            "cash_per_trade": cash_per_trade,
            "participation_rate": participation_rate,
            "strategy": "ma5_cross_ma20",
            "sample_end_policy": "mark_open_without_forced_exit" if end_date else "legacy_liquidate_if_sellable",
            "history_days": history_days,
        },
        "universe": sorted(bars_by_symbol.keys(), key=lambda k: k[0]),
        "skipped_symbols": skipped_symbols,
        "data_coverage": coverage,
        "signals": len(signals),
        "skipped_signals": result.skipped,
        "execution_basis": "independent_signal_daily_bar",
        "equity_basis": "initial_capital_then_daily_close",
        "pnl_basis": {"total_pnl": "realized_net", "unrealized_pnl": "mark_less_buy_outflow_excludes_exit_cost"},

        "initial_capital": result.initial_capital,
        "trades": [
            {**asdict(t), "entry_price": round(t.entry_price, 4), "exit_price": round(t.exit_price, 4)}
            for t in result.trades
        ],
        "open_positions": [
            {**asdict(p), "entry_price": round(p.entry_price, 4), "mark_price": round(p.mark_price, 4),
             "unrealized_pnl": round(p.unrealized_pnl, 4)}
            for p in result.open_positions
        ],
        "equity_curve": result.equity_curve,
        "equity_dates": result.equity_dates,
        "metrics": result.metrics,
    }
