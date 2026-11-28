"""Append-only storage for factual theme member observations."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.dialect import insert_ignore_sql


_INSERT = text(insert_ignore_sql(
    'factor_theme_observations', ('observation_id', 'trade_date', 'board_code', 'fetched_at', 'payload'),
    conflict_cols=('observation_id',),
))


def append_observations(db: Session, observations: list[dict]) -> tuple[list[str], int]:
    """Exact replays skip; distinct captures remain separate. Caller commits the batch."""
    refs: list[str] = []
    inserted = 0
    for observation in observations:
        payload = json.dumps(observation, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        ref = hashlib.sha256(payload.encode('utf-8')).hexdigest()
        with db.connection().execute(_INSERT, {
            'observation_id': ref, 'trade_date': observation['data_trade_date'],
            'board_code': observation['board_code'], 'fetched_at': observation['fetched_at'], 'payload': payload,
        }) as result:
            if result.rowcount not in (0, 1):
                raise RuntimeError('observation insert count unverified')
            inserted += result.rowcount
        refs.append(ref)
    return refs, inserted
