"""Collector coverage contract, using synthetic rows in the collector's actual shape."""
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
from src.core import theme_evolution_capture as capture


def run(coverage):
    return capture.plan_capture(
        now=datetime(2026, 11, 16, 15, 30, tzinfo=ZoneInfo('Asia/Shanghai')),
        fetch_boards=lambda:[{'code':'880001'}],
        fetch_members=lambda code:{'members':['000001.SZ'],'data_day':'20261116','date_basis':'source_reported'},
        fetch_pool=lambda day:[{'code':'000001.SZ','as_of':'2026-11-16','as_of_basis':'kline','pool_coverage':coverage}],
        source_fresh=True,source_trade_date='20261116')


@pytest.mark.parametrize('basis',["source_request",None,"unknown_source"])
def test_requested_date_alone_never_certifies_pool_data_day(basis):
    result=capture.plan_capture(
        now=datetime(2026,11,16,15,30,tzinfo=ZoneInfo('Asia/Shanghai')),
        fetch_boards=lambda:[{'code':'880001'}],
        fetch_members=lambda code:{'members':['000001.SZ'],'data_day':'20261116','date_basis':'source_reported'},
        fetch_pool=lambda day:[{'code':'000001.SZ','as_of':'2026-11-16','as_of_basis':basis,'pool_coverage':'complete'}],
        source_fresh=True,source_trade_date='20261116')
    assert result.pool_state=='unknown'
    assert result.pool_count is None


def test_declared_complete_collector_coverage_shape():
    result=run({'status':'complete','requested_codes':100,'fetched_codes':100,'batch_failures':0})
    assert result.status=='complete'


@pytest.mark.parametrize('coverage',[
    {'status':'complete','requested_codes':100,'fetched_codes':99,'batch_failures':0},
    {'status':'complete','requested_codes':100,'fetched_codes':100,'batch_failures':1},
    {'status':'complete'},
    {'status':'complete','requested_codes':0,'fetched_codes':0,'batch_failures':0},
    {'status':'complete','requested_codes':True,'fetched_codes':1,'batch_failures':0},
])
def test_invalid_coverage_not_complete(coverage):
    assert run(coverage).status=='partial'
