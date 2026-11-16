"""涨停池来源日期未核实或错日不得冒充当天。"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.core import theme_evolution as te
from src.db.models import ThemeBoardDaily, ThemeLimitUpDaily
from src.db.session import Base


@pytest.mark.parametrize('items,expected', [
    ([{'code': '000001', 'as_of': '2026-11-13'}], 'stale_day'),
    ([{'code': '000001'}], 'unknown_day'),
    ([{'code': '000001', 'as_of': '2026-11-16'}, {'code': '000002', 'as_of': '2026-11-13'}], 'unknown_day'),
    ([{'code': '000001', 'as_of': '2026-11-16', 'as_of_basis': 'source_request'}], 'unknown_day'),
    ([{'code': '000001', 'as_of': '2026-11-16'}], 'unknown_day'),
    ([], 'unavailable'),
])
def test_pool_date_not_proven_is_never_stored(items, expected):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine, tables=[ThemeBoardDaily.__table__, ThemeLimitUpDaily.__table__])
    with Session(engine) as db:
        result = te.collect_snapshot(
            db, day='20261116', now=datetime(2026, 11, 16, 15, 16, tzinfo=ZoneInfo('Asia/Shanghai')),
            source_fresh=True, source_trade_date='20261116',
            boards=[{'code': 'A1', 'name': '测试'}],
            fetch_members=lambda code: ['000001'], fetch_quotes=lambda codes: {},
            fetch_pool=lambda day: items,
        )
        assert result['limit_up_state'] == expected
        assert result['limit_up_stored'] is False
        assert db.execute(select(ThemeLimitUpDaily)).first() is None
        payload = te.build_payload(db, day='20261116', collect=result)
        assert payload['ranking_available'] is False
        assert payload['boards'][0]['limit_up_hits'] is None
    engine.dispose()


def test_same_day_pool_is_accepted_only_with_explicit_date():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine, tables=[ThemeBoardDaily.__table__, ThemeLimitUpDaily.__table__])
    with Session(engine) as db:
        result = te.collect_snapshot(
            db, day='20261116', now=datetime(2026, 11, 16, 15, 16, tzinfo=ZoneInfo('Asia/Shanghai')),
            source_fresh=True, source_trade_date='20261116',
            boards=[{'code': 'A1', 'name': '测试'}],
            fetch_members=lambda code: ['000001'], fetch_quotes=lambda codes: {},
            fetch_pool=lambda day: [{'code': '000001', 'as_of': '2026-11-16', 'as_of_basis': 'kline'}],
        )
        assert result['limit_up_state'] == 'ok'
        assert result['limit_up_stored'] is True
        assert db.execute(select(ThemeLimitUpDaily)).scalar_one().stock_code == '000001'
    engine.dispose()
