"""Counterexamples for source contract and literal preservation."""
from datetime import datetime
from zoneinfo import ZoneInfo
from src.core import theme_evolution_capture as cap

NOW=datetime(2026,11,16,15,16,tzinfo=ZoneInfo('Asia/Shanghai'))

def test_board_identifier_is_not_truncated_or_repaired():
    calls=[]
    out=cap.plan_capture(now=NOW, fetch_boards=lambda:[{'code':'X'*17},{'code':' A '}],
        fetch_members=lambda code:calls.append(code) or ['000001'],
        source_fresh=True,source_trade_date='20261116')
    assert not calls
    assert out.status == cap.STATUS_REFUSED


def test_unknown_pool_coverage_is_never_complete():
    out=cap.plan_capture(now=NOW, fetch_boards=lambda:[{'code':'A'}],
        fetch_members=lambda code:{'members':['000001'],'data_day':'20261116','date_basis':'source_reported'},
        fetch_pool=lambda day:[{'code':'000001','as_of':'20261116','as_of_basis':'kline'}],
        source_fresh=True, source_trade_date='20261116')
    assert out.status == cap.STATUS_PARTIAL
    assert 'pool_coverage_unknown' in out.notes


def test_structured_none_members_is_fetch_failed_not_empty():
    out=cap.plan_capture(now=NOW, fetch_boards=lambda:[{'code':'A'}],
        fetch_members=lambda code:{'members':None}, source_fresh=True,
        source_trade_date='20261116')
    assert out.boards[0].state == cap.STATE_FAILED
    assert out.boards[0].members is None


def test_different_market_suffixes_are_not_silently_merged():
    out=cap.plan_capture(now=NOW, fetch_boards=lambda:[{'code':'A'}],
        fetch_members=lambda code:['000001.SH','000001.SZ'], source_fresh=True,
        source_trade_date='20261116')
    assert out.boards[0].members == ('000001.SH','000001.SZ')


def test_wrong_target_date_does_not_call_trade_date_probe():
    calls=[]
    out=cap.plan_capture(now=NOW,day='20261113',fetch_boards=lambda:[],
        fetch_members=lambda code:[],fetch_trade_date=lambda:calls.append('called'))
    assert out.status == cap.STATUS_REFUSED
    assert not calls
