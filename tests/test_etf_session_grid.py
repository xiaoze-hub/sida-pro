from datetime import datetime
from src.core.etf_watch_feed import session_minute_grid, analyze_minutes, phase_of
from src.collectors.em_etf_minute import SESSION_MINUTES


def test_real_full_day_has_no_phantom_gap():
    grid = session_minute_grid('0931', '1500')
    assert len(grid) == 240
    assert set(grid) == set(SESSION_MINUTES)
    rows = [{'minute': m, 'amount_wan': 0, 'etfs': 20} for m in grid]
    assert analyze_minutes(rows, 20)['gaps'] == []


def test_midday_does_not_say_closed():
    now = datetime(2026, 11, 6, 12, 0)
    assert phase_of(now.date(), now) != 'closed'
