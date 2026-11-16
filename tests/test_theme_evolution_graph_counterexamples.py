import pytest
from src.core import theme_evolution_graph as graph


def event(day='2026-01-05', published=None):
    e={'event_id':'e','occurred_on':day,'theme_text':'X','source':'test',
       'instrument':{'id':'A','identity_status':'verified','identity_basis':'caller'}}
    if published is not None:
        e['published_at']=published
    return e


def test_offset_timestamp_future_in_shanghai_rejected():
    with pytest.raises(graph.GraphInputError) as err:
        graph.build_evidence_graph([event(published='2026-01-05T16:30:00+00:00')],as_of='2026-01-05')
    assert err.value.code=='future_published_at'


def test_instrument_node_discloses_caller_only_verification():
    out=graph.build_evidence_graph([event()],as_of='2026-01-05')
    node=next(n for n in out['nodes'] if n['node_id']=='instrument:A')
    assert node['identity_independently_verified'] is False


def test_unmatched_kline_reason_distinguishes_missing_cross_source_mapping():
    out=graph.build_evidence_graph([event()],klines=[{'id':'A','date':'2026-01-05','open':1,'high':1,'low':1,'close':1,'volume':1}],as_of='2026-01-05')
    assert out['unresolved']['klines_without_instrument'][0]['reason']=='cross_source_identity_basis_missing'


def test_calendar_derived_counts_retain_unverified_basis():
    e2=event('2026-01-07'); e2['event_id']='e2'
    out=graph.build_evidence_graph([event(),e2],calendar=['2026-01-05','2026-01-06','2026-01-07'],as_of='2026-01-07')
    assert out['segments'][1]['break_after_previous']['calendar_basis']=='supplied_by_caller_unverified'
    assert out['coverage']['unobserved_trading_days_in_span']['calendar_basis']=='supplied_by_caller_unverified'
    for edge in out['edges']:
        if edge['type'] in ('cycle_precedes_cycle','cycle_contains_theme','cycle_contains_instrument'):
            assert edge['support']['calendar_basis']=='supplied_by_caller_unverified'


def test_supplied_calendar_is_not_independently_verified():
    out=graph.build_evidence_graph([{'event_id':'e','occurred_on':'2026-01-05','theme_text':'X','source':'test',
        'instrument':{'id':'A','identity_status':'unverified'}}],calendar=['2026-01-05'],as_of='2026-01-05')
    assert out['segments'][0]['calendar_verified'] is False
    assert out['coverage']['continuity'] != 'calendar_verified'


def test_event_identity_does_not_verify_independent_kline_identity():
    out=graph.build_evidence_graph([{'event_id':'e','occurred_on':'2026-01-05','theme_text':'X','source':'event_source',
        'instrument':{'id':'A','identity_status':'verified','identity_basis':'event-source-only'}}],
        klines=[{'id':'A','date':'2026-01-05','open':1,'high':1,'low':1,'close':1,'volume':1}],as_of='2026-01-05')
    node=next(n for n in out['nodes'] if n['node_id']=='event:e')
    assert node['bar_present'] is None
