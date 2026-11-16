"""Independent checks for temporal and evidence provenance."""
from datetime import datetime
from zoneinfo import ZoneInfo
from src.core import theme_evolution_agent as agent


def test_generation_timestamp_is_unknown_if_not_observed():
    r=agent.run_once(now=datetime(2026,11,16,15,16,tzinfo=ZoneInfo('Asia/Shanghai')))
    assert r.generated_at is None
    assert r.generated_at_source == 'unknown'


def test_unlinked_timeline_does_not_claim_run_id():
    from dataclasses import replace
    r=agent.run_once(now=datetime(2026,11,16,15,16,tzinfo=ZoneInfo('Asia/Shanghai')))
    r=replace(r,run_id=42,timeline_state=agent.TIMELINE_OK,timeline={'timeline':[], 'provenance':{}})
    t=r.timeline_summary()
    assert t['run_id'] is None
    assert t['association'] == 'unverified_independent_input'
