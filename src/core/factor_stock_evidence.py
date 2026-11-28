"""Freeze what the primary database showed after close, not an inferred PIT history."""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timezone
from decimal import Decimal
from numbers import Integral, Real
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from src.db.factor_stock_evidence import FACTOR_FIELDS, load_stock_observations
from src.db.klines_repo import CN_TRADING_TZ

logger = logging.getLogger(__name__)
# Query deduplicates symbols, while each admitted roster serializes every member.
MAX_MEMBER_SLOTS = 6000
MAX_EVIDENCE_ROWS = 12_000
_SUPPORTED = re.compile(r'(?:(?:60|68)[0-9]{4}\.SH|(?:00|30)[0-9]{4}\.SZ)')


def capture_window(value: datetime, day: date) -> bool:
    """Shared gate: same Shanghai trading day at or after the close."""
    if value.utcoffset() is None:
        return False
    local = value.astimezone(CN_TRADING_TZ)
    return local.date() == day and local.time() >= time(15)


def _finite(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (Decimal, Real)):
        return None
    if isinstance(value, Integral):
        return int(value)
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _stamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError('a timestamp with an explicit offset is required')
    return value.astimezone(timezone.utc).isoformat()


def _row(member: str, raw: Mapping[str, Any] | None, day: date) -> dict[str, Any]:
    row: dict[str, Any] = dict(symbol=member, status='unsupported_identity', day_bar=None,
                               has_valid_traded_bar=None, factor_values=None, is_st=None,
                               trading_status='unknown', halt_record=None)
    if raw is None:
        return row
    row['status'] = 'checked'
    traded = False
    bar_ts = raw.get('bar_ts')
    if bar_ts is not None and (not isinstance(bar_ts, datetime) or bar_ts.utcoffset() is None
                               or bar_ts.astimezone(CN_TRADING_TZ).date() != day):
        row['status'] = 'bar_timestamp_unverified'
    elif isinstance(bar_ts, datetime):
        bar = {key: _finite(raw.get(key)) for key in ('open', 'high', 'low', 'close', 'volume')}
        o, h, l, c, v = (bar[key] for key in ('open', 'high', 'low', 'close', 'volume'))
        traded = (o is not None and h is not None and l is not None and c is not None
                  and v is not None and 0 < l <= min(o, c) <= max(o, c) <= h
                  and v > 0 and raw.get('quality_flag') == 1)
        row['day_bar'] = {**bar, 'source': raw['source'], 'ts': _stamp(bar_ts),
                          'trade_date': day.isoformat(), 'adjust': 'qfq', 'quality_flag': raw.get('quality_flag')}
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
    if not rosters:
        return []
    # Keep the global read budget but admit each roster whole. Smallest first
    # maximizes the number of complete rosters; ties retain input order.
    selected: list[int] = []
    read_symbols: set[str] = set()
    result: list[dict] = [{'status': 'no_supported_members', 'rows': None} for _ in rosters]
    remaining_rows = MAX_EVIDENCE_ROWS
    for i in sorted(range(len(rosters)), key=lambda idx: len(rosters[idx])):
        members = rosters[i]
        supported = {m[:6] for m in members if _SUPPORTED.fullmatch(m)}
        if not supported:
            continue
        if len(read_symbols | supported) > MAX_MEMBER_SLOTS or len(members) > remaining_rows:
            result[i]['status'] = 'member_budget_exceeded'
            continue
        selected.append(i)
        read_symbols.update(supported)
        remaining_rows -= len(members)
    symbols = sorted(read_symbols)

    def unavailable(reason: str) -> list[dict]:
        for i in selected:
            result[i]['status'] = reason
        return result

    if not symbols:
        return result
    started = now()
    if not capture_window(started, day) or started < earliest:
        return unavailable('clock_outside_capture_window')
    try:
        data = load_stock_observations(day, symbols)
    except (SQLAlchemyError, ValueError) as exc:
        logger.warning('逐股盘后证据读取失败: date=%s error=%s', day, type(exc).__name__)
        return unavailable('read_failed')
    finished = now()
    if not capture_window(finished, day) or finished < started:
        return unavailable('clock_outside_capture_window')
    if data is None:
        return unavailable('unsupported_backend')
    if set(data) != set(symbols):
        return unavailable('incomplete_read')
    for i in selected:
        rows = [_row(member, data[member[:6]] if _SUPPORTED.fullmatch(member) else None, day) for member in rosters[i]]
        # This is daily-bar/halt coverage for supported members, not proof of
        # complete membership, ST, factor lineage, or tradability.
        supported_rows = [row for row in rows if row['status'] != 'unsupported_identity']
        covered = sum(
            row['status'] == 'checked' and (
                row['trading_status'] == 'halt_recorded'
                or (row['has_valid_traded_bar'] is True and row['halt_record'] is None)
            ) for row in supported_rows
        )
        if covered == len(supported_rows):
            status = 'observed'
        elif covered or any(row['status'] == 'bar_timestamp_unverified'
                            or row['trading_status'] == 'conflicting_evidence' for row in supported_rows):
            status = 'partial'
        else:
            status = 'no_valid_day_bar'
        result[i] = {
            'status': status,
            'read_started_at': _stamp(started), 'read_finished_at': _stamp(finished),
            'source': 'primary_db_visible_rows', 'factor_lineage_verified': False,
            'supported_member_count': len(supported_rows),
            'day_bar_or_halt_covered': covered,
            'factor_rows_missing': sum(row['factor_values'] is None for row in supported_rows),
            'rows': rows,
        }
    return result
