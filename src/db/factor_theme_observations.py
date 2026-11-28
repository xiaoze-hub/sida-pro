"""Append-only storage for factual theme member observations."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.dialect import insert_ignore_sql
from src.db.session import pin_write_bind


_INSERT = text(insert_ignore_sql(
    'factor_theme_observations', ('observation_id', 'trade_date', 'board_code', 'fetched_at', 'payload'),
    conflict_cols=('observation_id',),
))
_INSERT_PG = text(_INSERT.text + ' RETURNING observation_id')


def append_observations(db: Session, observations: list[dict]) -> tuple[list[str], int]:
    """Exact replays skip; distinct captures remain separate. Caller commits the batch."""
    pin_write_bind(db)
    conn = db.connection()
    pg = conn.dialect.name == 'postgresql'
    refs: list[str] = []
    inserted = 0
    for observation in observations:
        payload = json.dumps(observation, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        ref = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        with conn.execute(_INSERT_PG if pg else _INSERT, {
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
