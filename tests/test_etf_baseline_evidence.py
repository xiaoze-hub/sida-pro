"""Regression checks using the actual shared ETF feed SQL."""
import sqlite3

from src.core import etf_watch_feed as feed


def _run_db(conn):
    def run(key, params, fmt=None):
        sql = feed.SQL[key].format(**(fmt or {}))
        return [dict(row) for row in conn.execute(sql, params)]
    return run


def test_baseline_excludes_null_amount_in_coverage():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE etf_minute_flow (trade_date TEXT, minute TEXT, etf_code TEXT, amount_wan REAL)')
    conn.executemany('INSERT INTO etf_minute_flow VALUES (?, ?, ?, ?)', [
        ('20260102', '0930', f'{i:06d}', None) for i in range(20)
    ])
    out = feed.baseline_curve(_run_db(conn), ['20260102'])
    assert out['days_used'] == 0
    assert out['cum'] == {}


def test_support_ignores_future_sc8_rows():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE etf_flow_daily (trade_date TEXT, scale_yi REAL, net_sub_yi REAL)')
    conn.executemany('INSERT INTO etf_flow_daily VALUES (?, ?, ?)', [
        ('20000101', 1, 2), ('99991231', 3, 4),
    ])
    out = feed.support_payload(_run_db(conn), 30)
    assert out['latest']['date'] == '20000101'
    assert out['sample_count'] == 1
