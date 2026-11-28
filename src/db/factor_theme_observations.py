"""Append-only storage for factual theme member observations."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from src.db.dialect import insert_ignore_sql
from src.db.session import pin_write_bind


_COLUMNS = ('observation_id', 'trade_date', 'board_code', 'fetched_at', 'stock_evidence_ready',
            'evidence_fingerprint', 'payload')
_INSERT = {
    dialect: text(insert_ignore_sql('factor_theme_observations', _COLUMNS, conflict_cols=('observation_id',))
                  + (' RETURNING observation_id' if dialect == 'postgresql' else ''))
    for dialect in ('postgresql', 'sqlite')
}
_LAST = text('''
    SELECT observation_id, evidence_fingerprint, fetched_at FROM factor_theme_observations
    WHERE trade_date = :trade_date AND board_code = :board_code
    ORDER BY fetched_at DESC, observation_id DESC LIMIT 1
''')


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _statement(dialect: str):
    """Pick SQL by the live connection's dialect, not the import-time process env."""
    if dialect in _INSERT:
        return _INSERT[dialect], dialect == 'postgresql'
    return text(insert_ignore_sql(
        'factor_theme_observations', _COLUMNS, conflict_cols=('observation_id',),
    )), False


def boards_with_stock_evidence(db: Session, trade_date: str, boards: set[str]) -> set[str]:
    """Read the daily-bar/halt retry flag, not factor or ranking readiness."""
    if not boards:
        return set()
    pin_write_bind(db)
    latest = text('''
        SELECT board_code FROM (
            SELECT board_code, stock_evidence_ready,
                   ROW_NUMBER() OVER (PARTITION BY board_code ORDER BY fetched_at DESC, observation_id DESC) AS recency
            FROM factor_theme_observations WHERE trade_date = :day AND board_code IN :boards
        ) AS ranked WHERE recency = 1 AND stock_evidence_ready
    ''').bindparams(bindparam('boards', expanding=True))
    with db.execute(latest, {'day': trade_date, 'boards': sorted(boards)}) as result:
        return set(result.scalars())


def append_observations(db: Session, observations: list[dict]) -> tuple[list[str], int]:
    """Append evidence changes; keep the original capture time on unchanged retries."""
    pin_write_bind(db)
    conn = db.connection()
    statement, pg = _statement(conn.dialect.name)
    refs: list[str] = []
    inserted = 0
    for observation in observations:
        capture = datetime.fromisoformat(observation['fetched_at'])
        if capture.utcoffset() is None:
            raise ValueError('observation capture requires a timezone offset')
        utc_capture = capture.astimezone(timezone.utc)
        observation = {**observation, 'fetched_at': utc_capture.isoformat()}
        # Fixed-width UTC key keeps indexed ordering independent of caller offset/precision.
        order_key = utc_capture.isoformat(timespec='microseconds')
        payload = _json(observation)
        ref = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        evidence = observation.get('stock_evidence') or {}
        stable = {key: value for key, value in observation.items() if key != 'fetched_at'}
        if isinstance(observation.get('stock_evidence'), dict):
            stable['stock_evidence'] = {key: value for key, value in evidence.items()
                                       if key not in ('read_started_at', 'read_finished_at')}
        fingerprint = hashlib.sha256(_json(stable).encode('utf-8')).hexdigest()
        identity = {'trade_date': observation['data_trade_date'], 'board_code': observation['board_code']}
        # Consecutive-state suppression is for the single scheduled writer. The
        # unique observation ID separately protects exact replays across connections.
        with conn.execute(_LAST, identity) as result:
            previous = result.one_or_none()
        if (previous is not None and previous.evidence_fingerprint == fingerprint
                and previous.fetched_at <= order_key):
            refs.append(previous.observation_id)
            continue
        # Per-row execution keeps each conflict outcome attributable to one observation.
        with conn.execute(statement, {
            **identity, 'observation_id': ref, 'fetched_at': order_key,
            'payload': payload, 'evidence_fingerprint': fingerprint,
            'stock_evidence_ready': observation.get('status') == 'observed'
                                     and evidence.get('status') in {'observed', 'partial_unsupported_members'},
        }) as result:
            if pg:
                saved = result.scalar_one_or_none()
                if saved is not None and saved != ref:
                    raise RuntimeError('observation insert identity unverified')
                inserted += int(saved is not None)
            else:
                if result.rowcount not in (0, 1):
                    raise RuntimeError('observation insert count unverified')
                inserted += result.rowcount
        refs.append(ref)
    return refs, inserted
