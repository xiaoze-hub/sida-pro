"""Scheduler must not silently discard skipped job results."""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from src.core import theme_evolution_scheduler as s


def test_scheduled_skipped_result_is_observable(monkeypatch, caplog):
    monkeypatch.setenv(s.ENV_FLAG,'1')
    obj=s.ThemeEvolutionScheduler(engine=object(), deps={'fetch_boards':lambda:[], 'fetch_members':lambda code:[]},
        upstream_ready=lambda now:{'ready':False,'day':'20261116'},
        now=lambda:datetime(2026,11,16,15,20,tzinfo=ZoneInfo('Asia/Shanghai')))
    with caplog.at_level(logging.INFO, logger=s.__name__):
        result=obj._job()
    assert result['status']=='skipped'
    assert 'upstream_not_ready' in caplog.text
