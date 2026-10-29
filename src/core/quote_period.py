"""Classify and label quote-backed daily P&L periods."""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from src.models.market import MARKETS, MarketCode

# 只有这两个期间能证明"这笔涨跌是哪一天的"。其余（无 quote_date ⇒ unknown）都不许进
# 汇总：2026-10-29 复审 —— 旧实现把 unknown 混进来，all-unknown 时标签写「当日盈亏」，
# 等于拿一笔"不知道是哪天"的涨跌冒充"今天赚了多少"。
TRUSTED_DAILY_PERIODS = frozenset({"today", "previous_trading_day"})


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
) -> dict[str, object]:
    """Build a truthful label/date for an account or mixed-market portfolio.

    `daily_pnl_excluded` = 有现价+昨收、但**日期证不出来**的篮子数。它们被排除在
    期间判定之外（也不计入汇总金额），下游可据此标「还有 N 条未纳入」。
    """
    items = list(observations)
    if not items:
        return {
            "daily_pnl_period": "unknown",
            "daily_pnl_label": "当日盈亏",
            "daily_pnl_date": None,
            "daily_pnl_excluded": 0,
        }

    usable = [(period, day) for period, day in items if period in TRUSTED_DAILY_PERIODS]
    excluded = len(items) - len(usable)
    periods = {period for period, _ in usable}
    dates = {value for _, value in usable if value}
    if periods == {"today"}:
        return {
            "daily_pnl_period": "today",
            "daily_pnl_label": "今日盈亏",
            "daily_pnl_date": next(iter(dates)) if len(dates) == 1 else None,
            "daily_pnl_excluded": excluded,
        }
    if periods == {"previous_trading_day"} and len(dates) == 1:
        return {
            "daily_pnl_period": "previous_trading_day",
            "daily_pnl_label": "上一交易日盈亏",
            "daily_pnl_date": next(iter(dates)),
            "daily_pnl_excluded": excluded,
        }
    return {
        "daily_pnl_period": "mixed" if len(periods) > 1 or len(dates) > 1 else "unknown",
        # 期间一条都证不出来时，标签必须是「期间未知」—— 绝不写「当日盈亏」。
        "daily_pnl_label": "最近交易日盈亏" if dates else "期间未知",
        "daily_pnl_date": None,
        "daily_pnl_excluded": excluded,
    }
