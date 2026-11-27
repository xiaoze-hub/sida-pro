"""市场日线回执按目标日真实落库覆盖，而非历史写入条数。"""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.core import klines_daily_refresh, klines_fullmarket, trading_calendar
from src.db import klines_repo


@pytest.mark.parametrize(('valid_count', 'status'), [(1, 'partial'), (2, 'complete'), (0, 'empty'), (None, 'unverified')])
def test_daily_job_emits_receipt_after_ingest(monkeypatch, tmp_path: Path, valid_count, status: str) -> None:
    from src.collectors import klines_ingestor, stock_list

    clock = datetime(2026, 11, 27, 16, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setattr(klines_daily_refresh, '_now_cn', lambda: clock, raising=False)
    monkeypatch.setattr(trading_calendar, 'is_trading_day', lambda _: True)
    monkeypatch.setattr(stock_list, 'get_stock_list', lambda: [{'symbol': '600001'}, {'symbol': '600002'}])
    monkeypatch.setattr(klines_fullmarket, 'limit_up_symbols', lambda: ['920001'])
    written: set[str] = set()

    async def ingest(_engine, symbol: str, *_args) -> dict:
        written.add(symbol)
        return {'ingested': 10}

    def coverage(_engine, trade_date, symbols: list[str]) -> int:
        assert written == {'600001', '600002', '920001'}
        assert trade_date.isoformat() == '2026-11-27'
        assert symbols == ['600001', '600002']
        if valid_count is None:
            raise TimeoutError
        return valid_count

    monkeypatch.setattr(klines_ingestor, 'ingest_symbol', ingest)
    monkeypatch.setattr(klines_repo, 'count_factor_ready_cn_bars', coverage, raising=False)
    stats = klines_daily_refresh.daily_job()
    assert stats['ok'] == 3
    receipt = stats['coverage_receipt']
    assert receipt['trade_date'] == '2026-11-27'
    assert receipt['status'] == status
    assert receipt['expected_count'] == 2
    assert receipt['valid_count'] == valid_count
    assert receipt['finished_at'] == clock.isoformat()
    path = tmp_path / 'reports' / 'kline_coverage' / '2026-11-27.json'
    assert json.loads(path.read_text(encoding='utf-8')) == receipt
