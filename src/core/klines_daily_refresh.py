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
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _now_cn() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _save_receipt(receipt: dict) -> None:
    """Atomic replace: readers must never see a half-written receipt."""
    dest = Path(os.environ.get("DATA_DIR") or "./data") / "reports" / "kline_coverage"
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{receipt['trade_date']}.json"
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=dest, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(receipt, tmp, ensure_ascii=False, sort_keys=True)
        tmp_path.replace(path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


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
        "universe": "stock_list_sh_sz_main_chinext_star",
        "caliber": "cn_qfq_tq_preferred_tencent_close_volume_quality",
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
    """Run factors after daily commits, then optionally record theme observations."""
    from src.collectors.factor_daily import collect_once
    from src.db.session import WriteSessionLocal

    stats = daily_job()
    board_config = os.environ.get("SIDA_FACTOR_THEME_OBSERVATION_BOARDS", "").strip()
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
            stats["theme_observation"] = {"written": 0, "reason": "factor_unavailable"}
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
        try:
            from src.core.factor_theme_observation import capture_after_daily_refresh

            stats["theme_observation"] = capture_after_daily_refresh(board_config, stats, now=_now_cn)
            if "reason" in stats["theme_observation"]:
                logger.warning("题材成员盘后观测跳过: date=%s reason=%s", target, stats["theme_observation"]["reason"])
        except Exception as exc:  # preserve upstream commits even if this independent write fails
            stats["theme_observation"] = {"written": 0, "reason": "capture_failed"}
            logger.error("题材成员盘后观测失败: date=%s error=%s", target, type(exc).__name__)
    return stats
