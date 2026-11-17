"""Evidence-bound adapter for the existing K-line response; no network calls."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.core.chan_kline_adapter import adapt_chart_bars


def test_current_bar_is_excluded_even_when_source_says_pg():
    payload = {
        'symbol': '000001', 'market': 'CN', 'interval': '1d', 'today_bar': 'pg',
        'klines': [
            {'date': '2026-11-16', 'open': 10, 'high': 11, 'low': 9, 'close': 10},
            {'date': '2026-11-17', 'open': 10, 'high': 12, 'low': 9, 'close': 11},
        ],
    }
    out = adapt_chart_bars(payload, symbol='000001', instrument='stock', now=datetime(2026, 11, 17, 14, tzinfo=ZoneInfo('Asia/Shanghai')))
    assert [b.day for b in out.bars] == ['2026-11-16']
    assert out.omitted_latest_reason == 'current_day_completion_unverified'
    assert out.source == 'unknown'
    assert out.adjustment == 'unknown'


def test_stock_index_collision_fails_closed():
    payload = {'symbol': '000001', 'market': 'CN', 'interval': '1d', 'is_index': True, 'klines': []}
    with pytest.raises(ValueError, match='instrument response mismatch'):
        adapt_chart_bars(payload, symbol='000001', instrument='stock', now=datetime.now(ZoneInfo('Asia/Shanghai')))


def test_future_and_partial_historical_bars_rejected():
    now = datetime(2026, 11, 17, 14, tzinfo=ZoneInfo('Asia/Shanghai'))
    base = {'symbol': '002361', 'market': 'CN', 'interval': '1d'}
    bar = {'date': '2026-11-18', 'open': 10, 'high': 11, 'low': 9, 'close': 10}
    with pytest.raises(ValueError, match='future bar'):
        adapt_chart_bars({**base, 'klines': [bar]}, symbol='002361', instrument='stock', now=now)
    with pytest.raises(ValueError, match='historical bar is partial'):
        adapt_chart_bars({**base, 'klines': [{**bar, 'date': '2026-11-16', 'partial': True}]}, symbol='002361', instrument='stock', now=now)


def test_day_cutoff_uses_shanghai_timezone():
    from datetime import timezone
    payload = {'symbol': '002361', 'market': 'CN', 'interval': '1d', 'klines': [
        {'date': '2026-11-17', 'open': 10, 'high': 11, 'low': 9, 'close': 10},
    ]}
    out = adapt_chart_bars(payload, symbol='002361', instrument='stock', now=datetime(2026, 11, 16, 16, 30, tzinfo=timezone.utc))
    assert out.bars == ()
    assert out.omitted_latest_reason == 'current_day_completion_unverified'


def test_naive_now_and_wrong_interval_rejected():
    payload = {'symbol': '002361', 'market': 'CN', 'interval': '1w', 'klines': []}
    with pytest.raises(ValueError, match='aware now'):
        adapt_chart_bars(payload, symbol='002361', instrument='stock', now=datetime(2026, 11, 17))
    with pytest.raises(ValueError, match='only CN daily'):
        adapt_chart_bars(payload, symbol='002361', instrument='stock', now=datetime(2026, 11, 17, tzinfo=ZoneInfo('Asia/Shanghai')))
