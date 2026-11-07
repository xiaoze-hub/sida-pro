"""Late ETF minutes and DB failures must not silently look successful."""
from datetime import datetime

from sqlalchemy import text

from src.collectors import tq_etf_watch as ew
from src.web.database import engine

pytest_plugins = ['tests.test_etf_watch']


def test_intraday_recovers_old_missing_and_revised_minutes(clean_flow, monkeypatch):
    monkeypatch.setattr(ew, '_now', lambda: datetime(2026, 11, 5, 14, 32))
    monkeypatch.setattr(ew, 'fetch_minute', lambda *a, **k: {
        '510300.SH': [('0930', 1.0, 10), ('1431', 2.0, 20)],
    })
    first = ew.run_intraday()
    assert first['ok']
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT minute, amount_wan FROM etf_minute_flow WHERE trade_date='20261105' ORDER BY minute")).all()
    assert rows == [('0930', 1.0), ('1431', 2.0)]
    monkeypatch.setattr(ew, 'fetch_minute', lambda *a, **k: {
        '510300.SH': [('0930', 9.0, 10), ('1431', 2.0, 20)],
    })
    assert ew.run_intraday()['ok']
    with engine.connect() as conn:
        assert conn.execute(text("SELECT amount_wan FROM etf_minute_flow WHERE trade_date='20261105' AND minute='0930' ")).scalar_one() == 9.0


def test_unchanged_rows_are_not_rewritten(clean_flow):
    data = {'510300.SH': [('0930', 1.0, 10)]}
    assert ew.write_minute('20261105', data) == 1
    with engine.begin() as conn:
        conn.execute(text("UPDATE etf_minute_flow SET updated_at='2000-01-01 00:00:00'"))
    assert ew.write_minute('20261105', data) == 1
    with engine.connect() as conn:
        assert str(conn.execute(text('SELECT updated_at FROM etf_minute_flow')).scalar_one()).startswith('2000-01-01')


def test_database_exception_does_not_report_success(monkeypatch):
    class BrokenEngine:
        def begin(self):
            raise RuntimeError('test database offline')

    monkeypatch.setattr('src.web.database.engine', BrokenEngine())
    assert ew.write_minute('20261105', {'510300.SH': [('0930', 1.0, 10)]}) == -1


def test_ingest_day_reports_database_failure(monkeypatch):
    monkeypatch.setattr(ew, 'fetch_minute', lambda *a, **k: {'510300.SH': [('0930', 1.0, 10)]})
    monkeypatch.setattr(ew, 'write_minute', lambda *a, **k: -1)
    assert ew.ingest_day('20261105')['ok'] is False
