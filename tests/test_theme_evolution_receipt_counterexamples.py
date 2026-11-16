"""Regression checks for readiness declarations, not claims of source truth."""
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine
from src.core import theme_evolution_receipt as r


def receipt(key, coverage, finished):
    return r.UpstreamReceipt(day='20261116',run_key=key,status=r.STATUS_OK,source='tq',
        started_at=finished-timedelta(minutes=2),finished_at=finished,
        source_data_day='20261116',date_basis='kline',close_complete=True,coverage=coverage)


def test_partial_coverage_cannot_be_declared_complete():
    engine=create_engine('sqlite://')
    r.create_schema(engine)
    r.append_receipt(engine,receipt('a','partial',datetime(2026,11,16,8,tzinfo=timezone.utc)))
    assert r.read_readiness(engine,'20261116').state != r.READINESS_COMPLETE_DECLARED


def test_unrecognized_coverage_cannot_be_declared_complete():
    engine=create_engine('sqlite://')
    r.create_schema(engine)
    r.append_receipt(engine,receipt('a','unknown',datetime(2026,11,16,8,tzinfo=timezone.utc)))
    assert r.read_readiness(engine,'20261116').state != r.READINESS_COMPLETE_DECLARED


def test_receipt_from_prior_day_cannot_claim_today_ready():
    engine=create_engine('sqlite://')
    r.create_schema(engine)
    r.append_receipt(engine,receipt('a','complete',datetime(2026,11,15,4,tzinfo=timezone.utc)))
    assert r.read_readiness(engine,'20261116').state != r.READINESS_COMPLETE_DECLARED


def test_out_of_order_completion_cannot_establish_readiness():
    engine=create_engine('sqlite://')
    r.create_schema(engine)
    t=datetime(2026,11,16,8,tzinfo=timezone.utc)
    r.append_receipt(engine,receipt('newer','complete',t))
    r.append_receipt(engine,receipt('older','complete',t-timedelta(minutes=10)))
    result=r.read_readiness(engine,'20261116')
    assert result.finished_out_of_order
    assert result.state != r.READINESS_COMPLETE_DECLARED
