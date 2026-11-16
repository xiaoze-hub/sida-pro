"""Independent counterexamples for experimental evidence storage."""
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text

from src.core import theme_evolution_store as store


def batch():
    return store.ObservationBatch(
        day='20261116', run_key='same-id', rule_version='v1', source='tq',
        captured_at=datetime(2026, 11, 16, 15, 16, tzinfo=ZoneInfo('Asia/Shanghai')),
        boards=(store.BoardEvidence(board_code='a', state=store.BOARD_OK, members=('000001',)),),
        pool_state=store.POOL_UNKNOWN,
    )


def test_same_run_key_changed_evidence_must_not_be_reported_as_retry():
    engine = create_engine('sqlite://')
    store.create_schema(engine)
    store.append_observation(engine, batch())
    changed = replace(batch(), boards=(store.BoardEvidence(board_code='a', state=store.BOARD_OK, members=('000002',)),))
    with pytest.raises(store.EvidenceConflictError):
        store.append_observation(engine, changed)
    engine.dispose()


def test_existing_caller_transaction_must_not_be_committed_or_rolled_back():
    engine = create_engine('sqlite://')
    store.create_schema(engine)
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE unrelated (v INTEGER)'))
    with engine.connect() as conn:
        conn.execute(text('INSERT INTO unrelated VALUES (42)'))
        with pytest.raises(store.EvidenceError):
            store.append_observation(conn, batch())
        assert conn.in_transaction()
        assert conn.execute(text('SELECT count(*) FROM unrelated')).scalar_one() == 1
        conn.rollback()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM unrelated')).scalar_one() == 0
    engine.dispose()


def test_latest_evidence_is_selected_by_capture_time_not_commit_order():
    engine = create_engine('sqlite://')
    store.create_schema(engine)
    newer = replace(batch(), run_key='newer', captured_at=datetime(2026, 11, 16, 16, 0, tzinfo=ZoneInfo('Asia/Shanghai')))
    older = replace(batch(), run_key='older', captured_at=datetime(2026, 11, 16, 15, 0, tzinfo=ZoneInfo('Asia/Shanghai')))
    store.append_observation(engine, newer)
    store.append_observation(engine, older)
    with engine.connect() as conn:
        assert store.latest_evidence(conn, '20261116')['run']['run_key'] == 'newer'
    engine.dispose()


def test_refused_attempts_do_not_exhaust_evidence_quota():
    engine = create_engine('sqlite://')
    store.create_schema(engine)
    for i in range(store.MAX_ATTEMPTS_PER_DAY):
        refused = replace(batch(), run_key=f'refused-{i}', boards=(),
                          pool_state=store.POOL_UNKNOWN, refused_reason='source_stale', source_fresh=False)
        assert store.append_observation(engine, refused).inserted
    result = store.append_observation(engine, replace(batch(), run_key='recovery'))
    assert result.inserted is True
    engine.dispose()


def test_member_string_is_not_interpreted_as_stock_sequence():
    engine = create_engine('sqlite://')
    store.create_schema(engine)
    bad = replace(batch(), boards=(store.BoardEvidence(board_code='a', state=store.BOARD_OK, members='012345'),))
    with pytest.raises(store.EvidenceError):
        store.append_observation(engine, bad)
    engine.dispose()


def test_capture_time_before_requested_day_cannot_be_complete():
    engine = create_engine('sqlite://')
    store.create_schema(engine)
    earlier = replace(batch(), captured_at=datetime(2026, 11, 13, 15, 16, tzinfo=ZoneInfo('Asia/Shanghai')))
    with pytest.raises(store.EvidenceError):
        store.append_observation(engine, earlier)
    engine.dispose()


def test_missing_board_effective_date_is_recorded_unknown_not_complete():
    engine = create_engine('sqlite://')
    store.create_schema(engine)
    data = replace(batch(), pool_state=store.POOL_OK, pool_data_day='20261116', pool_date_basis='kline',
                   limit_up=(store.LimitUpEvidence(stock_code='000001'),),
                   source_fresh=True, source_trade_date='20261116')
    out = store.append_observation(engine, data)
    assert out.status == store.STATUS_PARTIAL
    with engine.connect() as conn:
        row = store.evidence_for_run(conn, out.run_id)['boards'][0]
        assert row['data_day'] is None
        assert row['date_basis'] is None
    engine.dispose()


def test_missing_pool_never_claims_complete():
    engine = create_engine('sqlite://')
    store.create_schema(engine)
    result = store.append_observation(engine, batch())
    assert result.status == store.STATUS_PARTIAL
    engine.dispose()


def test_pool_ok_without_source_date_must_be_rejected():
    engine = create_engine('sqlite://')
    store.create_schema(engine)
    missing = replace(batch(), pool_state=store.POOL_OK,
                      limit_up=(store.LimitUpEvidence(stock_code='000001'),))
    with pytest.raises(store.EvidenceError):
        store.append_observation(engine, missing)
    engine.dispose()


def test_empty_ok_requires_explicit_empty_sequence():
    engine = create_engine('sqlite://')
    store.create_schema(engine)
    bad = replace(batch(), boards=(store.BoardEvidence(board_code='a', state=store.BOARD_EMPTY, members=None),))
    with pytest.raises(store.EvidenceError):
        store.append_observation(engine, bad)
    engine.dispose()
