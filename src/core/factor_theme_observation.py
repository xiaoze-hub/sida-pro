"""盘后留存实采板块成员。

允许留存已完成但覆盖为 partial 的回执，仅供观测；不代表日线、题材成员或排序输入完整。
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import date, datetime, timezone
from typing import Callable

from src.core import factor_stock_evidence, tdx_boards, trading_calendar
from src.core.klines_daily_refresh import COVERAGE_CALIBER, COVERAGE_UNIVERSE
from src.db.factor_theme_observations import append_observations
from src.db.session import WriteSessionLocal

logger = logging.getLogger(__name__)

_BOARD = re.compile(r'88[0-9]{4}\.SH')
_MEMBER = re.compile(r'[0-9]{6}\.(?:SH|SZ|BJ)')
MAX_BOARDS = 12
MAX_MEMBERS = 6000


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if result.utcoffset() is not None else None


def _capture_window(instant: datetime, trade_day: date) -> bool:
    return factor_stock_evidence.capture_window(instant, trade_day)


def _iso_day(raw: object) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    return day if day.isoformat() == raw else None


def factor_receipt_state(ready: object, now: datetime) -> str | None:
    """Reject corrupt, cross-day, or still-in-flight scheduler handoffs."""
    if not isinstance(ready, dict) or ready.get('version') != 1:
        return 'factor_receipt_unverified'
    coverage, factor = ready.get('coverage_receipt'), ready.get('factor_refresh')
    if not isinstance(coverage, dict) or not isinstance(factor, dict):
        return 'factor_receipt_unverified'
    day = _iso_day(coverage.get('trade_date'))
    finished = _timestamp(coverage.get('finished_at'))
    committed = _timestamp(ready.get('factor_finished_at'))
    if day is None or finished is None or committed is None or not _capture_window(committed, day):
        return 'factor_receipt_unverified'
    if not _capture_window(now, day):
        return 'clock_outside_capture_window'
    return None if finished <= committed <= now else 'factor_receipt_unverified'


def _coverage(receipt: dict, now: datetime) -> tuple[dict | None, str | None]:
    started, finished = (_timestamp(receipt.get(k)) for k in ('started_at', 'finished_at'))
    trade_day = _iso_day(receipt.get('trade_date'))
    if trade_day is None:
        return None, 'trade_date'
    valid, expected = receipt.get('valid_count'), receipt.get('expected_count')
    for key, expected_value in {
        'version': 1, 'caliber': COVERAGE_CALIBER, 'universe': COVERAGE_UNIVERSE,
    }.items():
        if receipt.get(key) != expected_value:
            return None, key
    if not started or not finished or not _capture_window(started, trade_day) or not _capture_window(finished, trade_day):
        return None, 'receipt_time'
    if not _capture_window(now, trade_day):
        return None, 'clock_outside_capture_window'
    if not started <= finished <= now:
        return None, 'receipt_time_order'
    if type(valid) is not int or type(expected) is not int or not 0 < valid <= expected:
        return None, 'coverage_counts'
    status = 'complete' if valid == expected else 'partial'
    if receipt.get('status') != status:
        return None, 'coverage_status'
    return {
        'trade_date': trade_day.isoformat(), 'status': status, 'valid_count': valid, 'expected_count': expected,
        'started_at': started.astimezone(timezone.utc).isoformat(),
        'finished_at': finished.astimezone(timezone.utc).isoformat(),
        'caliber': receipt['caliber'], 'universe': receipt['universe'],
    }, None


def _normalize_reply(reply: list[str] | None) -> tuple[str, list[str] | None]:
    if reply is None:
        return 'unavailable', None
    if not isinstance(reply, list) or len(reply) > MAX_MEMBERS or any(
        not isinstance(member, str) or not _MEMBER.fullmatch(member) for member in reply
    ):
        return 'invalid_response', None
    return ('observed', sorted(set(reply))) if reply else ('source_empty', [])


def configured_boards(board_config: str) -> set[str]:
    boards = {code.strip().upper() for code in board_config.split(',') if code.strip()}
    if (not 1 <= len(boards) <= MAX_BOARDS
            or any(not _BOARD.fullmatch(code) or code in tdx_boards.EXCLUDED_SECTORS for code in boards)):
        return set()
    return boards


def capture_after_daily_refresh(
    board_config: str, stats: dict, *, now: Callable[[], datetime],
) -> dict:
    """Consume only actual coverage/factor receipts from the scheduler; no backfill or ranking."""
    boards = configured_boards(board_config)
    if not boards:
        return {'written': 0, 'reason': 'invalid_board_config'}
    if stats.get('coverage_receipt_persisted') is not True:
        return {'written': 0, 'reason': 'receipt_not_persisted'}
    factor = stats.get('factor_refresh') or {}
    written = factor.get('written')
    if factor.get('reason') or type(written) is not int or written <= 0:
        return {'written': 0, 'reason': 'factor_unavailable', 'upstream_reason': factor.get('reason') or 'no_factor_rows'}
    begin = now()
    coverage, check = _coverage(stats.get('coverage_receipt') or {}, begin)
    if coverage is None:
        if check == 'clock_outside_capture_window':
            return {'written': 0, 'reason': check}
        return {'written': 0, 'reason': 'receipt_unverified', 'receipt_check': check}
    trade_day = date.fromisoformat(coverage['trade_date'])
    try:
        if not trading_calendar.is_trading_day(trade_day):
            return {'written': 0, 'reason': 'non_trading_day'}
    except trading_calendar.TradingCalendarError:
        return {'written': 0, 'reason': 'calendar_unverified'}

    # A board dayline probe is only a warning signal; it is never membership provenance.
    gateway_day = tdx_boards.latest_trade_date()
    last = now()
    if not factor_stock_evidence.capture_window(last, trade_day) or last < begin:
        return {'written': 0, 'reason': 'clock_outside_capture_window'}
    probe_day = _iso_day(gateway_day)
    gateway_status = ('same_day' if probe_day == trade_day else 'unknown' if probe_day is None
                      else 'stale' if probe_day < trade_day else 'future')
    observations = []
    for board in sorted(boards):
        status, members = _normalize_reply(tdx_boards.constituents(board))
        fetched = now()
        if not factor_stock_evidence.capture_window(fetched, trade_day) or fetched < last:
            return {'written': 0, 'reason': 'clock_outside_capture_window'}
        last = fetched
        observations.append({
            'version': 2, 'board_code': board, 'data_trade_date': trade_day.isoformat(),
            'fetched_at': fetched.astimezone(timezone.utc).isoformat(), 'source': 'tq_sector_members',
            'source_as_of': None, 'ranking_eligible': False, 'status': status, 'members': members,
            'membership_freshness': 'unverified', 'gateway_dayline_date': probe_day.isoformat() if probe_day else None,
            'gateway_dayline_status': gateway_status,
            'market_run': {'dayline_coverage': coverage, 'factor_rows_written': written},
        })
    # No DB write is open while reading the stock evidence or contacting the source.
    observed = [row for row in observations if row['status'] == 'observed']
    if gateway_status == 'same_day':
        snapshots = factor_stock_evidence.capture(
            trade_day, [row['members'] for row in observed], earliest=last, now=now,
        )
    else:
        reason = 'gateway_dayline_stale' if gateway_status == 'stale' else 'gateway_dayline_unverified'
        snapshots = [{'status': reason, 'rows': None} for _ in observed]
    # Absence of evidence must keep the member-level reason it came from.
    unobserved = {
        'unavailable': 'source_unavailable', 'source_empty': 'source_empty',
        'invalid_response': 'invalid_member_response',
    }
    for row in observations:
        row['stock_evidence'] = {'status': unobserved.get(row['status'], 'not_observed'), 'rows': None}
    for row, snapshot in zip(observed, snapshots, strict=True):
        row['stock_evidence'] = snapshot
    with WriteSessionLocal.begin() as db:
        observation_ids, inserted = append_observations(db, observations)
    member_counts = dict(Counter(row['status'] for row in observations))
    evidence_counts = dict(Counter(row['stock_evidence']['status'] for row in observations))
    degraded = (member_counts != {'observed': len(boards)} or evidence_counts != {'observed': len(boards)}
                or coverage['status'] != 'complete')
    logger.log(logging.WARNING if degraded else logging.INFO,
               '题材成员盘后观测已提交: date=%s inserted=%d coverage=%s members=%s evidence=%s',
               trade_day, inserted, coverage['status'], member_counts, evidence_counts)
    return {'written': inserted, 'observation_ids': observation_ids, 'status': 'degraded' if degraded else 'recorded',
            'data_trade_date': trade_day.isoformat(), 'coverage_status': coverage['status'],
            'ranking_eligible': False, 'membership_freshness': 'unverified',
            'gateway_dayline_status': gateway_status,
            'member_status_counts': member_counts, 'stock_evidence_status_counts': evidence_counts}
