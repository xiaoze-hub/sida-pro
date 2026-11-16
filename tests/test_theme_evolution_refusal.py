"""被拒绝的采集不得把旧数据伪装为新生成报告。"""
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from src.core import report_generator, theme_evolution as te
from src.db.models import ThemeBoardDaily, ThemeLimitUpDaily
from src.db.session import Base


def test_refused_collection_does_not_create_a_report(monkeypatch):
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine, tables=[ThemeBoardDaily.__table__, ThemeLimitUpDaily.__table__])
    writes = []
    monkeypatch.setattr(report_generator, 'write_report_document', lambda *a, **kw: writes.append(a))
    with Session(engine) as db:
        result = te.run_daily(
            db, day='20261113', now=datetime(2026, 11, 16, 15, 16, tzinfo=ZoneInfo('Asia/Shanghai')),
            source_fresh=True, source_trade_date='20261116', boards=[{'code': 'A1'}],
            fetch_members=lambda code: ['000001'], fetch_quotes=lambda codes: {}, fetch_pool=lambda day: [],
        )
        assert result['collect']['status'] == 'refused'
        assert result['report'] is None
        assert not writes
    engine.dispose()
