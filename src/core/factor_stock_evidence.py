"""Freeze what the primary database showed after close, not an inferred PIT history."""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from src.db.factor_stock_evidence import FACTOR_FIELDS, load_stock_observations
from src.db.klines_repo import CN_TRADING_TZ

logger = logging.getLogger(__name__)
# Bound stored entries including overlap between boards; never truncate a roster.
MAX_MEMBER_SLOTS = 1500
_SUPPORTED = re.compile(r'(?:(?:60|68)[0-9]{4}\.SH|(?:00|30)[0-9]{4}\.SZ)')


def _after_close(value: datetime, day: date) -> bool:
    if value.utcoffset() is None:
        return False
    local = value.astimezone(CN_TRADING_TZ)
    return local.date() == day and local.time() >= time(15)


def _finite(value: Any) -> float | int | None:
    return value if type(value) in (float, int) and math.isfinite(value) else None


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _row(member: str, raw: Mapping[str, Any] | None) -> dict[str, Any]:
    row: dict[str, Any] = dict(symbol=member, status='unsupported_identity', day_bar=None,
                               has_valid_traded_bar=None, factor_values=None, is_st=None,
                               trading_status='unknown', halt_record=None)
    if raw is None:
        return row
    row['status'] = 'checked'
    traded = False
    if raw.get('bar_ts') is not None:
        bar = {key: _finite(raw.get(key)) for key in ('open', 'high', 'low', 'close', 'volume')}
        o, h, l, c, v = (bar[key] for key in ('open', 'high', 'low', 'close', 'volume'))
        traded = (o is not None and h is not None and l is not None and c is not None
                  and v is not None and 0 < l <= min(o, c) <= max(o, c) <= h
                  and v > 0 and raw.get('quality_flag') == 1)
        row['day_bar'] = {**bar, 'source': raw['source'], 'ts': _stamp(raw['bar_ts']),
                          'adjust': 'qfq', 'quality_flag': raw.get('quality_flag')}
        row['has_valid_traded_bar'] = traded
    if raw.get('factor_id') is not None:
        row['factor_values'] = {key: _finite(raw.get(key)) for key in FACTOR_FIELDS}
    if raw.get('halt_id') is not None:
        row['halt_record'] = {key: raw[f'halt_{key}'] for key in ('start', 'end', 'source')}
        row['trading_status'] = 'conflicting_evidence' if traded else 'halt_recorded'
    return row


def capture(
    day: date, rosters: list[list[str]], *, earliest: datetime, now: Callable[[], datetime],
) -> list[dict]:
    """One bounded read across the observed rosters; missing fields are never imputed."""
    def unavailable(reason: str) -> list[dict]:
        return [{'status': reason, 'rows': None} for _ in rosters]

    if not rosters:
        return []
    if sum(map(len, rosters)) > MAX_MEMBER_SLOTS:
        return unavailable('member_budget_exceeded')
    symbols = sorted({m[:6] for members in rosters for m in members if _SUPPORTED.fullmatch(m)})
    started = now()
    if not _after_close(started, day) or started < earliest:
        return unavailable('clock_outside_capture_window')
    try:
        data = load_stock_observations(day, symbols) if symbols else {}
    except (SQLAlchemyError, ValueError) as exc:
        logger.warning('逐股盘后证据读取失败: date=%s error=%s', day, type(exc).__name__)
        return unavailable('read_failed')
    finished = now()
    if not _after_close(finished, day) or finished < started:
        return unavailable('clock_outside_capture_window')
    if data is None:
        return unavailable('unsupported_backend')
    if set(data) != set(symbols):
        return unavailable('incomplete_read')
    return [{
        'status': 'observed', 'read_started_at': _stamp(started), 'read_finished_at': _stamp(finished),
        'source': 'primary_db_visible_rows', 'factor_lineage_verified': False,
        'rows': [_row(member, data[member[:6]] if _SUPPORTED.fullmatch(member) else None) for member in members],
    } for members in rosters]
