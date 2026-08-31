"""Classify and label quote-backed daily P&L periods."""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from src.models.market import MARKETS, MarketCode


def classify_quote_period(
    quote_date: str | None,
    market: str,
    *,
    now: datetime | None = None,
) -> str:
    """Return today/previous_trading_day/unknown for a source quote date."""
    if not quote_date:
        return "unknown"
    try:
        source_date = date.fromisoformat(str(quote_date)[:10])
        market_def = MARKETS[MarketCode(market)]
    except (KeyError, TypeError, ValueError):
        return "unknown"

    current = now or datetime.now(market_def.get_tz())
    market_today = current.astimezone(market_def.get_tz()).date()
    if source_date == market_today:
        return "today"
    if source_date < market_today:
        return "previous_trading_day"
    return "unknown"


def summarize_daily_pnl_period(
    observations: Iterable[tuple[str, str | None]],
) -> dict[str, str | None]:
    """Build a truthful label/date for an account or mixed-market portfolio."""
    items = list(observations)
    if not items:
        return {
            "daily_pnl_period": "unknown",
            "daily_pnl_label": "当日盈亏",
            "daily_pnl_date": None,
        }

    periods = {period for period, _ in items}
    dates = {value for _, value in items if value}
    if periods == {"today"}:
        return {
            "daily_pnl_period": "today",
            "daily_pnl_label": "今日盈亏",
            "daily_pnl_date": next(iter(dates)) if len(dates) == 1 else None,
        }
    if periods == {"previous_trading_day"} and len(dates) == 1:
        return {
            "daily_pnl_period": "previous_trading_day",
            "daily_pnl_label": "上一交易日盈亏",
            "daily_pnl_date": next(iter(dates)),
        }
    return {
        "daily_pnl_period": "mixed" if len(periods) > 1 or len(dates) > 1 else "unknown",
        "daily_pnl_label": "最近交易日盈亏" if dates else "当日盈亏",
        "daily_pnl_date": None,
    }
