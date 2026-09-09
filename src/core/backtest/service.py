"""回测服务(B2.4/KI-039): 把内核接成"给标的+区间就出结果"的可调用入口。

策略口径(默认, 可调参):
- 信号: MA5 上穿 MA20(金叉), **仅用截至当日的收盘价**, 无未来函数;
- 入场: 信号次日开盘(内核规则); 止损/止盈: 信号日收盘 × (1∓pct);
- 撮合: 涨跌停不可成交 + 成交量参与率上限(内核 B0.2 规则)。
"""

from __future__ import annotations

from dataclasses import asdict

from src.core.backtest.data_adapter import load_price_history
from src.core.backtest.engine import Backtester, Signal

MIN_BARS = 30
MAX_SYMBOLS = 20


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
    """对给定标的跑金叉策略回测, 返回 trades/净值曲线/指标。"""
    mkt = (market or "CN").strip().upper() or "CN"
    bars_by_symbol: dict = {}
    signals: list[Signal] = []
    skipped_symbols: list[str] = []
    for sym in list(symbols)[:MAX_SYMBOLS]:
        code = str(sym or "").strip()
        if not code:
            continue
        bars = load_price_history(code, mkt, days=days)
        if len(bars) < MIN_BARS:
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
        },
        "universe": sorted(bars_by_symbol.keys(), key=lambda k: k[0]),
        "skipped_symbols": skipped_symbols,
        "signals": len(signals),
        "skipped_signals": result.skipped,
        "trades": [asdict(t) for t in result.trades],
        "equity_curve": result.equity_curve,
        "equity_dates": result.equity_dates,
        "metrics": result.metrics,
    }
