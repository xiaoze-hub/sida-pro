"""A 观察层不得把当前板块成分伪装成历史或未来数据。"""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.core import theme_evolution as te
from src.db.models import ThemeBoardDaily, ThemeLimitUpDaily
from src.db.session import Base


def test_current_constituents_cannot_be_written_for_another_day():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine, tables=[ThemeBoardDaily.__table__, ThemeLimitUpDaily.__table__])
    with Session(engine) as db:
        for day in ('20261113', '20261117'):
            calls = []
            result = te.collect_snapshot(
                db, day=day, now=datetime(2026, 11, 16, 15, 16, tzinfo=ZoneInfo('Asia/Shanghai')),
                source_fresh=True, source_trade_date='20261116',
                boards=[{'code': 'A1', 'name': '测试'}],
                fetch_members=lambda code: calls.append(code) or ['000001'],
                fetch_quotes=lambda codes: {}, fetch_pool=lambda day: [],
            )
            assert result['status'] == 'refused'
            assert result['reason'] == 'current_members_wrong_day'
            assert not calls
            assert db.execute(select(ThemeBoardDaily)).first() is None
            assert db.execute(select(ThemeLimitUpDaily)).first() is None
    engine.dispose()
