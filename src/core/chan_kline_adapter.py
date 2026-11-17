"""Conservative adapter of a chart response into completed daily raw bars.

Pure conversion; caller must fetch with explicit instrument, and use the same
response for chart and structure. This cannot prove source finality or PIT.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from src.core.chan_structure import RawBar, detect_raw_extrema


@dataclass(frozen=True)
class ChartBars:
    bars: tuple[RawBar, ...]
    source: str
    adjustment: str
    omitted_latest_reason: str | None


def adapt_chart_bars(
    payload: dict,
    *,
    symbol: str,
    instrument: Literal['stock', 'index'],
    now: datetime,
) -> ChartBars:
    """Adapt explicitly identified daily data; current day is never presumed final."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('aware now required')
    if instrument not in ('stock', 'index') or payload.get('symbol') != symbol:
        raise ValueError('instrument identity mismatch')
    if payload.get('market') != 'CN' or payload.get('interval') != '1d':
        raise ValueError('only CN daily bars supported')
    if (payload.get('is_index') is True) != (instrument == 'index'):
        raise ValueError('instrument response mismatch')
    rows = payload.get('klines')
    if not isinstance(rows, list):
        raise ValueError('missing klines')
    today = now.astimezone(ZoneInfo('Asia/Shanghai')).date()
    result = []
    omitted = None
    previous = None
    for row in rows:
        try:
            if not isinstance(row, dict):
                raise ValueError('invalid bar')
            day = date.fromisoformat(row['date'])
            if previous is not None and day <= previous:
                raise ValueError('bar dates must be strictly increasing')
            previous = day
            if day > today:
                raise ValueError('future bar')
            if day == today:
                omitted = 'current_day_completion_unverified'
                continue
            if row.get('partial') is True:
                raise ValueError('historical bar is partial')
            result.append(RawBar(day=day.isoformat(), open=row['open'], high=row['high'], low=row['low'], close=row['close']))
        except (KeyError, TypeError):
            raise ValueError('invalid bar') from None
    detect_raw_extrema(result)  # Existing pure validation, no output used.
    source = payload.get('source')
    return ChartBars(tuple(result), source if isinstance(source, str) and source else 'unknown', 'unknown', omitted)
