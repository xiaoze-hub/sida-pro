from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from src.collectors import factor_daily
from src.core import klines_daily_refresh as refresh, tdx_boards
from src.db.session import WriteSessionLocal


THEME = '880741.SH'
NOW = datetime(2026, 11, 27, 8, 35, tzinfo=timezone.utc)


def install_upstream(monkeypatch, *, receipt=None, persisted=True, written=2):
    from src.core import trading_calendar

    default = {'version': 1, 'trade_date': '2026-11-27', 'started_at': '2026-11-27T16:00:00+08:00',
               'finished_at': '2026-11-27T16:30:00+08:00', 'expected_count': 10, 'valid_count': 7,
               'status': 'partial', 'caliber': 'cn_qfq_tq_preferred_tencent_close_volume_quality',
               'universe': 'stock_list_sh_sz_main_chinext_star'}
    monkeypatch.setattr(refresh, 'daily_job', lambda: {'coverage_receipt': default if receipt is None else receipt,
                                                     'coverage_receipt_persisted': persisted})
    monkeypatch.setattr(refresh, '_now_cn', lambda: NOW)
    monkeypatch.setattr(trading_calendar, 'is_trading_day', lambda _: True)
    monkeypatch.setattr(tdx_boards, 'latest_trade_date', lambda: '2026-11-27')
    monkeypatch.setattr(factor_daily, 'collect_once', lambda *_a, **_k: {'written': written, 'skipped': 0})


def stored_rows():
    with WriteSessionLocal() as db:
        return [(row.observation_id, json.loads(row.payload)) for row in db.execute(text(
            'SELECT observation_id, payload FROM factor_theme_observations ORDER BY fetched_at, observation_id'
        ))]


@pytest.fixture(autouse=True)
def _clean_observations(monkeypatch):
    monkeypatch.delenv('SIDA_FACTOR_THEME_OBSERVATION_BOARDS', raising=False)
    yield
    with WriteSessionLocal() as db:
        if inspect(db.connection()).has_table('factor_theme_observations'):
            db.execute(text('DELETE FROM factor_theme_observations'))
            db.commit()


@pytest.mark.parametrize('reply,state,members', [
    (None, 'unavailable', None),
    ([], 'source_empty', []),
    (['600001.SH', '000001.SZ', '600001.SH'], 'observed', ['000001.SZ', '600001.SH']),
    (['600001.SH', 'bad'], 'invalid_response', None),
])
def test_daily_job_persists_observation_without_authorizing_a_rank(monkeypatch, reply, state, members):
    install_upstream(monkeypatch)
    monkeypatch.setenv('SIDA_FACTOR_THEME_OBSERVATION_BOARDS', THEME)
    calls = []

    def constituents(board):
        calls.append(board)
        return reply

    monkeypatch.setattr(tdx_boards, 'constituents', constituents)
    result = refresh.daily_job_with_factors()
    assert calls == [THEME]
    assert result['theme_observation']['written'] == 1
    (ref, doc), = stored_rows()
    assert result['theme_observation']['observation_ids'] == [ref]
    assert doc['board_code'] == THEME
    assert doc['data_trade_date'] == '2026-11-27'
    assert doc['fetched_at'] == NOW.isoformat()
    assert doc['source_as_of'] is None
    assert doc['ranking_eligible'] is False
    assert doc['status'] == state
    assert doc['members'] == members
    assert doc['dayline_coverage']['status'] == 'partial'
    assert doc['version'] == 2
    expected_evidence = {'observed': 'unsupported_backend', 'unavailable': 'source_unavailable',
                         'source_empty': 'source_empty', 'invalid_response': 'invalid_member_response'}[state]
    assert doc['stock_evidence'] == {'status': expected_evidence, 'rows': None}
    assert result['theme_observation']['ranking_eligible'] is False
    assert result['theme_observation']['gateway_dayline_status'] == 'same_day'


@pytest.mark.parametrize('selector,persisted,written,expected_reason', [
    ('', True, 2, 'disabled'),
    (THEME, False, 2, 'receipt_not_persisted'),
    (THEME, True, 0, 'factor_unavailable'),
    ('880741.ＳＨ', True, 2, 'invalid_board_config'),
    ('880741.SH,880742.SH,880743.SH,880744.SH,880745.SH,880746.SH,880747.SH,'
     '880748.SH,880749.SH,880750.SH,880751.SH,880752.SH,880753.SH', True, 2, 'invalid_board_config'),
])
def test_ineligible_run_does_not_fetch_members(monkeypatch, selector, persisted, written, expected_reason):
    install_upstream(monkeypatch, persisted=persisted, written=written)
    if selector:
        monkeypatch.setenv('SIDA_FACTOR_THEME_OBSERVATION_BOARDS', selector)
    monkeypatch.setattr(tdx_boards, 'constituents', lambda _: pytest.fail('unqualified work must not fetch'))
    status = refresh.daily_job_with_factors().get('theme_observation', {'reason': 'disabled'})
    assert status['reason'] == expected_reason
    assert stored_rows() == []


def test_lowercase_board_config_is_normalized(monkeypatch):
    install_upstream(monkeypatch)
    monkeypatch.setenv('SIDA_FACTOR_THEME_OBSERVATION_BOARDS', '880741.sh')
    seen = []
    monkeypatch.setattr(tdx_boards, 'constituents', lambda code: seen.append(code) or ['000001.SZ'])
    result = refresh.daily_job_with_factors()['theme_observation']
    assert seen == [THEME]
    assert result['written'] == 1


def test_same_evidence_idempotent_later_retrieval_preserves_both(monkeypatch):
    install_upstream(monkeypatch)
    monkeypatch.setenv('SIDA_FACTOR_THEME_OBSERVATION_BOARDS', THEME)
    monkeypatch.setattr(tdx_boards, 'constituents', lambda _: ['600001.SH'])
    first = refresh.daily_job_with_factors()['theme_observation']
    same = refresh.daily_job_with_factors()['theme_observation']
    assert same['written'] == 0
    assert first['observation_ids'] == same['observation_ids']
    monkeypatch.setattr(refresh, '_now_cn', lambda: NOW + timedelta(minutes=1))
    monkeypatch.setattr(tdx_boards, 'constituents', lambda _: ['600002.SH'])
    second = refresh.daily_job_with_factors()['theme_observation']
    assert second['written'] == 1
    assert [doc['members'] for _, doc in stored_rows()] == [['600001.SH'], ['600002.SH']]


def test_capture_finishing_after_shanghai_date_change_is_not_written(monkeypatch):
    install_upstream(monkeypatch)
    monkeypatch.setenv('SIDA_FACTOR_THEME_OBSERVATION_BOARDS', THEME)
    clocks = iter([NOW, NOW, NOW + timedelta(hours=9)])
    monkeypatch.setattr(refresh, '_now_cn', lambda: next(clocks))
    monkeypatch.setattr(tdx_boards, 'constituents', lambda _: ['600001.SH'])
    status = refresh.daily_job_with_factors()['theme_observation']
    assert status['reason'] == 'clock_outside_capture_window'
    assert stored_rows() == []


@pytest.mark.parametrize('key,bad', [
    ('trade_date', '2026-11-26'), ('started_at', '2026-11-27T16:00:00'),
    ('finished_at', '2026-11-27T16:37:00+08:00'), ('status', 'complete'),
    ('valid_count', True), ('caliber', 'unknown'),
])
def test_bad_upstream_receipt_never_reads_gateway(monkeypatch, key, bad):
    install_upstream(monkeypatch)
    upstream = refresh.daily_job()
    upstream['coverage_receipt'][key] = bad
    monkeypatch.setattr(refresh, 'daily_job', lambda: upstream)
    monkeypatch.setenv('SIDA_FACTOR_THEME_OBSERVATION_BOARDS', THEME)
    monkeypatch.setattr(tdx_boards, 'constituents', lambda _: pytest.fail('bad receipt fetched members'))
    assert refresh.daily_job_with_factors()['theme_observation']['reason'] == 'receipt_unverified'
    assert stored_rows() == []


def test_write_error_rolls_back_all_observations_and_preserves_upstream_result(monkeypatch, caplog):
    from src.core import factor_theme_observation as capture

    install_upstream(monkeypatch)
    monkeypatch.setenv('SIDA_FACTOR_THEME_OBSERVATION_BOARDS', f'{THEME},880742.SH')
    monkeypatch.setattr(tdx_boards, 'constituents', lambda _: ['600001.SH'])
    append = capture.append_observations

    def fail_after_write(db, observations):
        append(db, observations[:1])
        raise RuntimeError('private diagnostic must not be logged')

    monkeypatch.setattr(capture, 'append_observations', fail_after_write)
    result = refresh.daily_job_with_factors()
    assert result['factor_refresh']['written'] == 2
    assert result['theme_observation'] == {'written': 0, 'reason': 'capture_failed'}
    assert stored_rows() == []
    assert 'private diagnostic must not be logged' not in caplog.text


def test_read_failure_does_not_discard_theme_members(monkeypatch, caplog):
    from src.core import factor_stock_evidence

    install_upstream(monkeypatch)
    monkeypatch.setenv('SIDA_FACTOR_THEME_OBSERVATION_BOARDS', THEME)
    monkeypatch.setattr(tdx_boards, 'constituents', lambda _: ['000001.SZ'])

    def fail(*_):
        raise SQLAlchemyError('private diagnostic')

    monkeypatch.setattr(factor_stock_evidence, 'load_stock_observations', fail)
    assert refresh.daily_job_with_factors()['theme_observation']['written'] == 1
    (_, row), = stored_rows()
    assert row['ranking_eligible'] is False
    assert row['members'] == ['000001.SZ']
    assert row['stock_evidence'] == {'status': 'read_failed', 'rows': None}
    assert 'private diagnostic' not in caplog.text


def test_stale_gateway_is_recorded_not_presented_as_current(monkeypatch):
    install_upstream(monkeypatch)
    monkeypatch.setenv('SIDA_FACTOR_THEME_OBSERVATION_BOARDS', THEME)
    monkeypatch.setattr(tdx_boards, 'latest_trade_date', lambda: '2026-11-26')
    monkeypatch.setattr(tdx_boards, 'constituents', lambda _: ['000001.SZ'])
    result = refresh.daily_job_with_factors()['theme_observation']
    assert result['written'] == 1
    assert result['status'] == 'degraded'
    assert result['member_status_counts'] == {'observed': 1}
    assert result['stock_evidence_status_counts'] == {'gateway_dayline_stale': 1}
    (_, row), = stored_rows()
    assert row['source_as_of'] is None
    assert row['gateway_dayline_date'] == '2026-11-26'
    assert row['membership_freshness'] == 'unverified'
    assert row['stock_evidence']['status'] == 'gateway_dayline_stale'
