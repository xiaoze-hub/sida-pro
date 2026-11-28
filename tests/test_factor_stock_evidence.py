from datetime import date, datetime, timedelta, timezone

import pytest

from src.core import factor_stock_evidence as evidence

DAY = date(2026, 11, 27)
NOW = datetime(2026, 11, 27, 8, 35, tzinfo=timezone.utc)
MEMBERS = ['000001.SZ', '000002.SZ', '000003.SZ', '600001.SH', '600001.SZ', '920001.BJ']


def test_freezes_each_member_without_conflating_chart_and_trade_status(monkeypatch):
    price = {'bar_ts': NOW - timedelta(hours=8), 'source': 'tq', 'open': 10.0, 'high': 11.0,
             'low': 9.0, 'close': 10.1, 'volume': 200, 'quality_flag': 1}
    raw = {
        '000001': {**price, 'factor_id': 12, 'momentum_20': 2.5, 'volume_z20': float('nan')},
        '000002': {**price, 'volume': 0, 'halt_id': 2, 'halt_start': '2026-11-26',
                   'halt_end': '', 'halt_source': 'manual'},
        '000003': {**price, 'halt_id': 3, 'halt_start': '2026-11-27', 'halt_end': '2026-11-27',
                   'halt_source': 'announcement'},
        '600001': {},
    }

    def read(day, symbols):
        assert day == DAY
        assert symbols == ['000001', '000002', '000003', '600001']
        return raw

    monkeypatch.setattr(evidence, 'load_stock_observations', read)
    result = evidence.capture(DAY, [MEMBERS, ['000001.SZ']], earliest=NOW, now=lambda: NOW)
    batch, other = result
    assert batch['status'] == 'partial'
    assert batch['read_finished_at'] == NOW.isoformat()
    assert batch['factor_lineage_verified'] is False
    rows = {row['symbol']: row for row in batch['rows']}
    assert list(rows) == MEMBERS
    assert rows['000001.SZ']['day_bar']['volume'] == 200
    assert rows['000001.SZ']['day_bar']['trade_date'] == DAY.isoformat()
    assert batch['factor_rows_missing'] == 3
    assert batch['supported_member_count'] == 4
    assert rows['000001.SZ']['factor_values']['momentum_20'] == 2.5
    assert rows['000001.SZ']['factor_values']['volume_z20'] is None
    assert rows['000001.SZ']['has_valid_traded_bar'] is True
    assert rows['000001.SZ']['trading_status'] == 'unknown'
    assert rows['000002.SZ']['has_valid_traded_bar'] is False
    assert rows['000002.SZ']['trading_status'] == 'halt_recorded'
    assert rows['000003.SZ']['trading_status'] == 'conflicting_evidence'
    assert rows['600001.SH']['day_bar'] is None
    assert rows['600001.SH']['factor_values'] is None
    assert rows['600001.SZ']['status'] == 'unsupported_identity'
    assert rows['920001.BJ']['status'] == 'unsupported_identity'
    assert all(row['is_st'] is None for row in rows.values())
    raw['000001']['momentum_20'] = -9
    assert other['rows'][0]['factor_values']['momentum_20'] == 2.5


def test_known_halt_resolves_daily_bar_gap_but_unsupported_members_stay_unknown(monkeypatch):
    monkeypatch.setattr(evidence, 'load_stock_observations', lambda *_: {
        '000001': {'halt_id': 1, 'halt_start': '2026-11-27', 'halt_end': '', 'halt_source': 'manual'},
    })
    batch, = evidence.capture(DAY, [['000001.SZ', '920001.BJ']], earliest=NOW, now=lambda: NOW)
    assert batch['status'] == 'partial_unsupported_members'
    assert batch['unsupported_member_count'] == 1
    assert batch['rows'][0]['trading_status'] == 'halt_recorded'
    assert batch['rows'][1]['trading_status'] == 'unknown'


@pytest.mark.parametrize('result, expected', [(None, 'unsupported_backend'), ({}, 'incomplete_read')])
def test_no_positive_evidence_when_backend_returns_incomplete_set(monkeypatch, result, expected):
    monkeypatch.setattr(evidence, 'load_stock_observations', lambda *_: result)
    batch, = evidence.capture(DAY, [['000001.SZ']], earliest=NOW, now=lambda: NOW)
    assert batch == {'status': expected, 'rows': None}


def test_small_roster_survives_large_roster_without_truncation(monkeypatch):
    monkeypatch.setattr(evidence, 'MAX_MEMBER_SLOTS', 1)
    def read(_day, symbols):
        assert symbols == ['000001']
        return {'000001': {}}

    monkeypatch.setattr(evidence, 'load_stock_observations', read)
    large, small, second = evidence.capture(
        DAY, [['000002.SZ', '000003.SZ'], ['000001.SZ'], ['000002.SZ']], earliest=NOW, now=lambda: NOW,
    )
    assert large == second == {'status': 'member_budget_exceeded', 'rows': None}
    assert small['status'] == 'no_valid_day_bar' and len(small['rows']) == 1


def test_only_unsupported_members_never_claim_a_database_read(monkeypatch):
    monkeypatch.setattr(evidence, 'load_stock_observations', lambda *_: pytest.fail('must not read'))
    batch, = evidence.capture(DAY, [['920001.BJ']], earliest=NOW, now=lambda: NOW)
    assert batch == {'status': 'no_supported_members', 'rows': None}


def test_overlapping_boards_only_charge_new_symbols_to_the_read_budget(monkeypatch):
    monkeypatch.setattr(evidence, 'MAX_MEMBER_SLOTS', 2)

    def read(_day, symbols):
        assert symbols == ['000001', '000002']
        return {symbol: {} for symbol in symbols}

    monkeypatch.setattr(evidence, 'load_stock_observations', read)
    rosters = [['000001.SZ'], ['000001.SZ', '000002.SZ']]
    batches = evidence.capture(DAY, rosters, earliest=NOW, now=lambda: NOW)
    assert [[row['symbol'] for row in batch['rows']] for batch in batches] == rosters


def test_read_dedup_does_not_remove_per_roster_payload_budget(monkeypatch):
    monkeypatch.setattr(evidence, 'MAX_EVIDENCE_ROWS', 2)
    monkeypatch.setattr(evidence, 'load_stock_observations', lambda _day, symbols: {symbol: {} for symbol in symbols})
    roster = ['000001.SZ', '920001.BJ']
    first, second = evidence.capture(DAY, [roster, roster], earliest=NOW, now=lambda: NOW)
    assert [row['symbol'] for row in first['rows']] == roster
    assert second == {'status': 'member_budget_exceeded', 'rows': None}


def test_decimal_and_naive_timestamp_are_kept_separate(monkeypatch):
    from decimal import Decimal

    price = {'bar_ts': NOW, 'source': 'tq', 'open': Decimal('10.1'), 'high': Decimal('11'),
             'low': Decimal('9'), 'close': Decimal('10.2'), 'volume': 200, 'quality_flag': 1,
             'factor_id': 2, 'momentum_20': Decimal('2.5'), 'vol_20': True}
    monkeypatch.setattr(evidence, 'load_stock_observations', lambda *_: {
        '000001': price, '000002': {**price, 'bar_ts': NOW.replace(tzinfo=None)},
    })
    batch, = evidence.capture(DAY, [['000001.SZ', '000002.SZ']], earliest=NOW, now=lambda: NOW)
    good, unknown = batch['rows']
    assert good['day_bar']['close'] == 10.2 and good['has_valid_traded_bar'] is True
    assert good['factor_values']['vol_20'] is None
    assert batch['status'] == 'partial'
    assert unknown['status'] == 'bar_timestamp_unverified'
    assert unknown['day_bar'] is None and unknown['has_valid_traded_bar'] is None
    assert unknown['factor_values']['momentum_20'] == 2.5


def test_postgres_projection_matches_persisted_factor_contract():
    from sqlalchemy.dialects import postgresql
    from src.db import factor_stock_evidence as repo
    from src.db.models import StockFactorDaily

    assert set(repo.FACTOR_FIELDS) <= set(StockFactorDaily.__table__.columns.keys())
    stmt = repo._READ.compile(dialect=postgresql.dialect())
    assert set(stmt.params) == {'symbols', 'lo', 'hi', 'iso_date', 'compact_date'}
    for clause in ("market = 'CN'", "period = '1d'", "adjust = 'qfq'", "source IN ('tq', 'tencent')"):
        assert clause in stmt.string


def test_insert_statement_follows_live_dialect():
    from src.db import factor_theme_observations as store

    assert 'RETURNING observation_id' in store._statement('postgresql')[0].text
    sqlite_statement, is_pg = store._statement('sqlite')
    assert not is_pg and 'RETURNING' not in sqlite_statement.text


@pytest.mark.parametrize('finished', [NOW - timedelta(seconds=1), NOW + timedelta(hours=8)])
def test_late_or_reversed_clock_cannot_authorize_read(monkeypatch, finished):
    monkeypatch.setattr(evidence, 'load_stock_observations', lambda *_: {'000001': {}})
    clock = iter((NOW, finished))
    batch, = evidence.capture(DAY, [['000001.SZ']], earliest=NOW, now=lambda: next(clock))
    assert batch == {'status': 'clock_outside_capture_window', 'rows': None}
