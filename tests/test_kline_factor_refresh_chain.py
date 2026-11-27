from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.collectors import factor_daily
from src.core import klines_daily_refresh as refresh
from src.db import session


@pytest.mark.parametrize('usable', [True, False])
def test_scheduled_factor_refresh_waits_for_verified_bar_ingest(monkeypatch, usable: bool) -> None:
    calls: list[str] = []
    db = object()

    def kline_job() -> dict:
        calls.append('klines committed')
        return {'coverage_receipt': {'trade_date': '2026-11-27', 'valid_count': 2 if usable else 0}}

    def collect(got_db: object, *, trade_date: str) -> dict:
        assert got_db is db
        calls.append(trade_date)
        return {'written': 2, 'skipped': 0}

    monkeypatch.setattr(refresh, 'daily_job', kline_job)
    monkeypatch.setattr(session, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(factor_daily, 'collect_once', collect)
    refresh.daily_job_with_factors()
    assert calls == (['klines committed', '20261127'] if usable else ['klines committed'])


def test_receipt_disk_failure_preserves_ingest_stats(monkeypatch, tmp_path: Path) -> None:
    from src.collectors import klines_ingestor, stock_list
    from src.core import klines_fullmarket, trading_calendar
    from src.db import klines_repo

    clock = datetime(2026, 11, 27, 16, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
    monkeypatch.setattr(refresh, '_now_cn', lambda: clock)
    monkeypatch.setattr(trading_calendar, 'is_trading_day', lambda _: True)
    monkeypatch.setattr(stock_list, 'get_stock_list', lambda: [{'symbol': '600001'}])
    monkeypatch.setattr(klines_fullmarket, 'limit_up_symbols', list)
    async def ingest(*_args: object) -> dict:
        return {'ingested': 1}
    monkeypatch.setattr(klines_ingestor, 'ingest_symbol', ingest)
    monkeypatch.setattr(klines_repo, 'count_factor_ready_cn_bars', lambda *_args: 1)
    path = tmp_path / 'not_a_directory'
    path.touch()
    monkeypatch.setenv('DATA_DIR', str(path))
    stats = refresh.daily_job()
    assert stats['ok'] == 1
    assert stats['coverage_receipt']['valid_count'] == 1
    assert stats['coverage_receipt_persisted'] is False
