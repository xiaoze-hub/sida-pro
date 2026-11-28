"""盘后留存实采板块成员。

允许留存已完成但覆盖为 partial 的回执，仅供观测；不代表日线、题材成员或排序输入完整。
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from src.core import tdx_boards, trading_calendar
from src.db.factor_theme_observations import append_observations
from src.db.session import WriteSessionLocal

logger = logging.getLogger(__name__)
CN = ZoneInfo('Asia/Shanghai')
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
    if instant.utcoffset() is None:
        return False
    local = instant.astimezone(CN)
    return local.date() == trade_day and local.time() >= time(15)


def _coverage(receipt: dict, now: datetime) -> dict | None:
    started, finished = (_timestamp(receipt.get(k)) for k in ('started_at', 'finished_at'))
    try:
        raw_day = receipt.get('trade_date')
        if not isinstance(raw_day, str):
            return None
        trade_day = date.fromisoformat(raw_day)
    except ValueError:
        return None
    valid, expected = receipt.get('valid_count'), receipt.get('expected_count')
    if (receipt.get('version') != 1 or trade_day.isoformat() != raw_day
            or receipt.get('caliber') != 'cn_qfq_tq_preferred_tencent_close_volume_quality'
            or receipt.get('universe') != 'stock_list_sh_sz_main_chinext_star'
            or not started or not finished or not _capture_window(now, trade_day)
            or not _capture_window(started, trade_day) or not started <= finished <= now
            or type(valid) is not int or type(expected) is not int or not 0 < valid <= expected):
        return None
    status = 'complete' if valid == expected else 'partial'
    if receipt.get('status') != status:
        return None
    return {
        'trade_date': raw_day, 'status': status, 'valid_count': valid, 'expected_count': expected,
        'started_at': started.astimezone(timezone.utc).isoformat(),
        'finished_at': finished.astimezone(timezone.utc).isoformat(),
        'caliber': receipt['caliber'], 'universe': receipt['universe'],
    }


def _normalize_reply(reply: list[str] | None) -> tuple[str, list[str] | None]:
    if reply is None:
        return 'unavailable', None
    if not isinstance(reply, list) or len(reply) > MAX_MEMBERS or any(
        not isinstance(member, str) or not _MEMBER.fullmatch(member) for member in reply
    ):
        return 'invalid_response', None
    return ('observed', sorted(set(reply))) if reply else ('source_empty', [])


def capture_after_daily_refresh(
    board_config: str, stats: dict, *, now: Callable[[], datetime],
) -> dict:
    """Consume only actual coverage/factor receipts from the scheduler; no backfill or ranking."""
    boards = {code.strip() for code in board_config.split(',')}
    if (not 1 <= len(boards) <= MAX_BOARDS
            or any(not _BOARD.fullmatch(code) or code in tdx_boards.EXCLUDED_SECTORS for code in boards)):
        return {'written': 0, 'reason': 'invalid_board_config'}
    if stats.get('coverage_receipt_persisted') is not True:
        return {'written': 0, 'reason': 'receipt_not_persisted'}
    factor = stats.get('factor_refresh') or {}
    written = factor.get('written')
    if factor.get('reason') or type(written) is not int or written <= 0:
        return {'written': 0, 'reason': 'factor_unavailable'}
    begin = now()
    coverage = _coverage(stats.get('coverage_receipt') or {}, begin)
    if coverage is None:
        return {'written': 0, 'reason': 'receipt_unverified'}
    trade_day = date.fromisoformat(coverage['trade_date'])
    try:
        if not trading_calendar.is_trading_day(trade_day):
            return {'written': 0, 'reason': 'non_trading_day'}
    except trading_calendar.TradingCalendarError:
        return {'written': 0, 'reason': 'calendar_unverified'}

    observations = []
    last = begin
    for board in sorted(boards):
        status, members = _normalize_reply(tdx_boards.constituents(board))
        fetched = now()
        if not _capture_window(fetched, trade_day) or fetched < last:
            return {'written': 0, 'reason': 'clock_outside_capture_window'}
        last = fetched
        observations.append({
            'version': 1, 'board_code': board, 'data_trade_date': trade_day.isoformat(),
            'fetched_at': fetched.astimezone(timezone.utc).isoformat(), 'source': 'tq_sector_members',
            'source_as_of': None, 'ranking_eligible': False, 'status': status, 'members': members,
            'dayline_coverage': coverage, 'factor_rows_written': written,
        })
    with WriteSessionLocal.begin() as db:
        observation_ids, inserted = append_observations(db, observations)
    logger.info('题材成员盘后观测已提交: date=%s batches=%d inserted=%d', trade_day, len(boards), inserted)
    return {'written': inserted, 'observation_ids': observation_ids}
