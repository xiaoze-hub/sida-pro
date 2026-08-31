from datetime import datetime
from zoneinfo import ZoneInfo

from src.core.quote_period import classify_quote_period, summarize_daily_pnl_period


def test_quote_from_previous_cn_trading_day_is_not_today():
    now = datetime(2026, 8, 10, 8, 24, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert classify_quote_period("2026-08-07", "CN", now=now) == "previous_trading_day"


def test_quote_from_current_market_date_is_today():
    now = datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert classify_quote_period("2026-08-10", "CN", now=now) == "today"


def test_previous_trading_day_summary_exposes_date():
    meta = summarize_daily_pnl_period([
        ("previous_trading_day", "2026-08-07"),
        ("previous_trading_day", "2026-08-07"),
    ])

    assert meta == {
        "daily_pnl_period": "previous_trading_day",
        "daily_pnl_label": "上一交易日盈亏",
        "daily_pnl_date": "2026-08-07",
    }


def test_mixed_quote_dates_use_recent_trading_day_label():
    meta = summarize_daily_pnl_period([
        ("today", "2026-08-10"),
        ("previous_trading_day", "2026-08-07"),
    ])

    assert meta["daily_pnl_period"] == "mixed"
    assert meta["daily_pnl_label"] == "最近交易日盈亏"
    assert meta["daily_pnl_date"] is None
