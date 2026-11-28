# -*- coding: utf-8 -*-
"""全市场日线(qfq)每日增量 job(v0.5.87)。

交易日 16:00 由 startup 调度: 对全 A 股跑 days=10 的增量 upsert(ON CONFLICT 自愈),
保持连板天梯/逐股日K 的全市场覆盖新鲜。取数走既有 ingest_symbol, 不新造取数链。
同步入口供 APScheduler 线程池调用(asyncio.run 于线程内)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, time
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from apscheduler.schedulers.base import BaseScheduler

logger = logging.getLogger(__name__)
_THEME_BOARDS_ENV = 'SIDA_FACTOR_THEME_OBSERVATION_BOARDS'
_CN_TZ = ZoneInfo('Asia/Shanghai')
_THEME_FINAL_ATTEMPT = time(23, 45)
_THEME_RETRY_MINUTE = 45
_THEME_MISFIRE_SECONDS = 300
COVERAGE_UNIVERSE = 'stock_list_sh_sz_main_chinext_star'
COVERAGE_CALIBER = 'cn_qfq_tq_preferred_tencent_close_volume_quality'


def _now_cn() -> datetime:
    return datetime.now(_CN_TZ)


def _report_path(report: str, day: date) -> Path:
    return Path(os.environ.get('DATA_DIR') or './data') / 'reports' / report / f'{day.isoformat()}.json'


def _save_json(path: Path, receipt: dict) -> None:
    """Atomic replace: readers must never see a half-written receipt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(receipt, tmp, ensure_ascii=False, sort_keys=True)
        tmp_path.replace(path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _save_receipt(receipt: dict) -> None:
    _save_json(_report_path('kline_coverage', date.fromisoformat(receipt['trade_date'])), receipt)


def daily_job(days: int = 10, concurrency: int = 8) -> dict:
    from src.collectors.klines_ingestor import ingest_symbol
    from src.collectors.stock_list import get_stock_list
    from src.core.klines_fullmarket import (
        a_share_universe,
        limit_up_symbols,
        merge_universe,
        run_backfill,
    )
    from src.db.session import engine
    from src.db.klines_repo import count_factor_ready_cn_bars
    from src.core.trading_calendar import TradingCalendarError, is_trading_day
    from src.models.market import MarketCode

    started = _now_cn()
    main_universe = a_share_universe(get_stock_list())
    universe = merge_universe(main_universe, limit_up_symbols())

    async def ingest(symbol: str, d: int) -> dict:
        return await ingest_symbol(engine, symbol, MarketCode.CN, "1d", d)

    stats = asyncio.run(
        run_backfill(universe, days=days, ingest=ingest, concurrency=concurrency)
    )
    logger.info("全市场日线增量完成: universe=%d ok=%d fail=%d",
                len(universe), stats["ok"], stats["fail"])
    if started.time() < time(15):
        stats["coverage_skipped"] = "before_close"
        return stats
    try:
        if not is_trading_day(started.date()):
            stats["coverage_skipped"] = "non_trading_day"
            return stats
    except TradingCalendarError:
        stats["coverage_skipped"] = "calendar_unverified"
        logger.warning("市场日线覆盖无法核验：缺交易日历 date=%s", started.date())
        return stats

    expected = len(main_universe)
    valid: int | None = None
    try:
        candidate = count_factor_ready_cn_bars(engine, started.date(), main_universe)
        if 0 <= candidate <= expected:
            valid = candidate
    except Exception as exc:  # noqa: BLE001 - coverage failure remains explicitly unknown
        logger.warning("市场日线覆盖无法核验: %s", type(exc).__name__)
    status = ("universe_empty" if not expected else "unverified" if valid is None else
              "empty" if not valid else "complete" if valid == expected else "partial")
    receipt = {
        "version": 1,
        "trade_date": started.date().isoformat(),
        "started_at": started.isoformat(),
        "finished_at": _now_cn().isoformat(),
        "expected_count": expected,
        "valid_count": valid,
        "status": status,
        "universe": COVERAGE_UNIVERSE,
        "caliber": COVERAGE_CALIBER,
        "ingest_ok_count": stats["ok"],
        "ingest_failed_count": stats["fail"],
    }
    stats["coverage_receipt"] = receipt
    try:
        _save_receipt(receipt)
        stats["coverage_receipt_persisted"] = True
    except OSError as exc:
        stats["coverage_receipt_persisted"] = False
        logger.error("市场日线覆盖回执落盘失败: date=%s error=%s", receipt["trade_date"], type(exc).__name__)
    logger.info("市场日线覆盖回执: date=%s status=%s valid=%s expected=%d",
                receipt["trade_date"], status, valid, expected)
    return stats


def daily_job_with_factors() -> dict:
    """Run factors after daily commits; never contact the board gateway here."""
    from src.collectors.factor_daily import collect_once
    from src.db.session import WriteSessionLocal

    stats = daily_job()
    board_config = os.environ.get(_THEME_BOARDS_ENV, '').strip()
    receipt = stats.get("coverage_receipt") or {}
    coverage = {
        "coverage_status": receipt.get("status", "not_attempted"),
        "coverage_valid_count": receipt.get("valid_count"),
        "coverage_expected_count": receipt.get("expected_count"),
    }
    if not receipt.get("valid_count"):
        reason = stats.get("coverage_skipped") or {
            "empty": "target_bars_empty",
            "universe_empty": "universe_empty",
        }.get(coverage["coverage_status"], "target_bars_unverified")
        stats["factor_refresh"] = {"written": 0, "reason": reason, **coverage}
        if board_config:
            stats["theme_observation"] = {"written": 0, "reason": "factor_unavailable", "upstream_reason": reason}
            logger.info("题材成员盘后观测未执行: date=%s upstream=%s", receipt.get("trade_date"), reason)
        if reason not in ("before_close", "non_trading_day"):
            logger.warning("个股因子日快照跳过: date=%s reason=%s", receipt.get("trade_date"), reason)
        return stats

    target = date.fromisoformat(receipt["trade_date"]).strftime("%Y%m%d")
    try:
        with WriteSessionLocal() as db:
            stats["factor_refresh"] = {**collect_once(db, trade_date=target), **coverage}
    except Exception as exc:  # noqa: BLE001 - preserve actual upstream result
        stats["factor_refresh"] = {"written": 0, "reason": "collect_failed", **coverage}
        logger.error("个股因子日快照计算失败: date=%s error=%s", target, type(exc).__name__)
    if board_config:
        factor = stats['factor_refresh']
        written = factor.get('written')
        if stats.get('coverage_receipt_persisted') is not True:
            preparation = {'written': 0, 'reason': 'receipt_not_persisted'}
        elif factor.get('reason') or type(written) is not int or written <= 0:
            preparation = {'written': 0, 'reason': 'factor_unavailable',
                           'upstream_reason': factor.get('reason') or 'no_factor_rows'}
        else:
            ready = {'version': 1, 'factor_finished_at': _now_cn().isoformat(),
                     'coverage_receipt': receipt, 'coverage_receipt_persisted': True,
                     'factor_refresh': {'written': written}}
            try:
                _save_json(_report_path('factor_theme_ready', date.fromisoformat(receipt['trade_date'])), ready)
                preparation = {'written': 0, 'reason': 'queued_for_observation'}
            except OSError as exc:
                preparation = {'written': 0, 'reason': 'factor_receipt_write_failed'}
                logger.warning('题材观测就绪回执写入失败: date=%s error=%s', target, type(exc).__name__)
        stats['theme_observation'] = preparation
        logger.info('题材观测就绪状态: date=%s reason=%s', target, preparation['reason'])
    return stats


def register_theme_observation(scheduler: BaseScheduler) -> bool:
    if not os.environ.get(_THEME_BOARDS_ENV, '').strip():
        return False
    from apscheduler.triggers.cron import CronTrigger

    # Same-day receipt only; the tail runs late so a slow upstream is not lost.
    trigger = CronTrigger(day_of_week='mon-fri', hour='17-23', minute='*/15', timezone=_CN_TZ)
    from src.core.factor_theme_observation import configured_boards

    if not configured_boards(os.environ[_THEME_BOARDS_ENV]):
        logger.error('盘后题材成员观测配置无效, 未注册: %s', _THEME_BOARDS_ENV)
        return False
    scheduler.add_job(
        daily_theme_observation_job, trigger, id='factor-theme-observation', name='盘后题材成员观测',
        replace_existing=True, max_instances=1, coalesce=True, misfire_grace_time=_THEME_MISFIRE_SECONDS,
    )
    return True


def daily_theme_observation_job() -> dict:
    """Separate optional gateway workload; requires a completed factor run today."""
    from src.core.factor_theme_observation import capture_after_daily_refresh, configured_boards, factor_receipt_state
    from src.core.trading_calendar import TradingCalendarError, is_trading_day
    from src.db.factor_theme_observations import latest_board_evidence
    from src.db.session import WriteSessionLocal

    config = os.environ.get(_THEME_BOARDS_ENV, '').strip()
    if not config:
        return {'written': 0, 'reason': 'disabled'}
    boards = configured_boards(config)
    if not boards:
        logger.warning('题材成员盘后观测未配置有效板块代码')
        return {'written': 0, 'reason': 'invalid_board_config'}
    now = _now_cn()
    day = now.date()
    try:
        if not is_trading_day(day):
            return {'written': 0, 'reason': 'non_trading_day'}
    except TradingCalendarError:
        logger.warning('题材成员盘后观测无交易日历: date=%s', day)
        return {'written': 0, 'reason': 'calendar_unverified'}
    try:
        with _report_path('factor_theme_ready', day).open(encoding='utf-8') as file:
            receipt = json.load(file)
    except FileNotFoundError:
        result = {'written': 0, 'reason': 'factor_receipt_missing'}
    except (OSError, ValueError):
        result = {'written': 0, 'reason': 'factor_receipt_unreadable'}
    else:
        reason = factor_receipt_state(receipt, now)
        if reason:
            result = {'written': 0, 'reason': reason}
        else:
            try:
                with WriteSessionLocal() as db:
                    state = latest_board_evidence(db, day.isoformat(), boards)
            except Exception as exc:  # the source was never contacted; do not blame capture
                result = {'written': 0, 'reason': 'evidence_lookup_failed'}
                logger.error('题材成员证据状态查询失败: date=%s error=%s', day, type(exc).__name__)
            else:
                pending = {board for board in boards if not state.get(board)}
                if not pending:
                    return {'written': 0, 'reason': 'observation_already_committed'}
                # First capture can consume a late receipt at any tick. Once any
                # observation is stored, retry incomplete boards at :45 (plus grace),
                # not at every receipt poll. An unknown halt is still not coverage.
                minute = now.astimezone(_CN_TZ).minute
                if not _THEME_RETRY_MINUTE <= minute <= _THEME_RETRY_MINUTE + _THEME_MISFIRE_SECONDS // 60:
                    pending -= state.keys()
                    if not pending:
                        return {'written': 0, 'reason': 'deferred_board_retry'}
                try:
                    result = capture_after_daily_refresh(','.join(sorted(pending)), receipt, now=_now_cn)
                except Exception as exc:  # evidence failure cannot disturb committed factors
                    result = {'written': 0, 'reason': 'capture_failed'}
                    logger.error('题材成员盘后观测失败: date=%s error=%s', day, type(exc).__name__)
    if 'reason' in result:
        waiting = result['reason'] == 'factor_receipt_missing' and now.astimezone(_CN_TZ).time() < _THEME_FINAL_ATTEMPT
        logger.log(logging.INFO if waiting else logging.WARNING,
                   '题材成员盘后观测未完成: date=%s reason=%s receipt_check=%s',
                   day, result['reason'], result.get('receipt_check', '-'))
    return result
