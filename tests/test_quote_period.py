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
        "daily_pnl_excluded": 0,
    }


def test_all_unknown_dates_are_not_labelled_as_today_pnl():
    """一条日期都证不出来时，标签必须是「期间未知」——不许写「当日盈亏」。

    2026-10-29 复审：旧实现落到最后一个分支，dates 为空 ⇒ label 直接写「当日盈亏」，
    把"不知道是哪天的涨跌"冒充成"今天赚了多少"。
    """
    meta = summarize_daily_pnl_period([("unknown", None), ("unknown", None)])

    assert meta["daily_pnl_period"] == "unknown"
    assert meta["daily_pnl_label"] == "期间未知"
    assert meta["daily_pnl_date"] is None
    assert meta["daily_pnl_excluded"] == 2


def test_unknown_dates_are_excluded_from_a_known_period_summary():
    """不能证明日期的篮子不进摘要（否则「今日盈亏」里混进一笔不知道哪天的涨跌）。"""
    meta = summarize_daily_pnl_period([
        ("today", "2026-08-10"),
        ("unknown", None),
    ])

    assert meta["daily_pnl_period"] == "today"
    assert meta["daily_pnl_label"] == "今日盈亏"
    assert meta["daily_pnl_date"] == "2026-08-10"
    assert meta["daily_pnl_excluded"] == 1


def test_mixed_quote_dates_use_recent_trading_day_label():
    meta = summarize_daily_pnl_period([
        ("today", "2026-08-10"),
        ("previous_trading_day", "2026-08-07"),
    ])

    assert meta["daily_pnl_period"] == "mixed"
    assert meta["daily_pnl_label"] == "最近交易日盈亏"
    assert meta["daily_pnl_date"] is None
