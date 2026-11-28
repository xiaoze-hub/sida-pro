"""Append-only storage for factual theme member observations."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from src.db.dialect import insert_ignore_sql
from src.db.session import pin_write_bind


_COLUMNS = ('observation_id', 'trade_date', 'board_code', 'fetched_at', 'payload')
_INSERT = {
    dialect: text(insert_ignore_sql('factor_theme_observations', _COLUMNS, conflict_cols=('observation_id',))
                  + (' RETURNING observation_id' if dialect == 'postgresql' else ''))
    for dialect in ('postgresql', 'sqlite')
}


def _statement(dialect: str):
    """Pick SQL by the live connection's dialect, not the import-time process env."""
    if dialect in _INSERT:
        return _INSERT[dialect], dialect == 'postgresql'
    return text(insert_ignore_sql(
        'factor_theme_observations', _COLUMNS, conflict_cols=('observation_id',),
    )), False


def boards_with_stock_evidence(db: Session, trade_date: str, boards: set[str]) -> set[str]:
    """Read the latest committed record for each requested board on the writer."""
    if not boards:
        return set()
    pin_write_bind(db)
    latest = text('''
        SELECT board_code, payload FROM (
            SELECT board_code, payload,
                   ROW_NUMBER() OVER (PARTITION BY board_code ORDER BY fetched_at DESC, observation_id DESC) AS recency
            FROM factor_theme_observations WHERE trade_date = :day AND board_code IN :boards
        ) AS ranked WHERE recency = 1
    ''').bindparams(bindparam('boards', expanding=True))
    complete: set[str] = set()
    with db.execute(latest, {'day': trade_date, 'boards': sorted(boards)}) as result:
        for code, payload in result:
            doc = json.loads(payload)
            if doc.get('status') == 'observed' and (doc.get('stock_evidence') or {}).get('status') == 'observed':
                complete.add(code)
    return complete


def append_observations(db: Session, observations: list[dict]) -> tuple[list[str], int]:
    """Exact replays skip; distinct captures remain separate. Caller commits the batch."""
    pin_write_bind(db)
    conn = db.connection()
    statement, pg = _statement(conn.dialect.name)
    refs: list[str] = []
    inserted = 0
    for observation in observations:
        payload = json.dumps(observation, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        ref = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        # Per-row execution keeps each conflict outcome attributable to one observation.
        with conn.execute(statement, {
            'observation_id': ref, 'trade_date': observation['data_trade_date'],
            'board_code': observation['board_code'], 'fetched_at': observation['fetched_at'], 'payload': payload,
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
