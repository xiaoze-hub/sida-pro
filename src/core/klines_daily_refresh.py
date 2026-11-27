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
    from src.core.trading_calendar import is_trading_day
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
    if started.time() < time(15) or not is_trading_day(started.date()):
        return stats

    expected = len(main_universe)
    valid: int | None = None
    try:
        candidate = count_factor_ready_cn_bars(engine, started.date(), main_universe)
        if 0 <= candidate <= expected:
            valid = candidate
    except Exception as exc:  # noqa: BLE001 - coverage failure remains explicitly unknown
        logger.warning("市场日线覆盖无法核验: %s", type(exc).__name__)
    status = ("unverified" if valid is None or not expected else
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
    """Run the legacy factor snapshot only after the upstream run has committed."""
    from src.collectors.factor_daily import collect_once
    from src.db.session import SessionLocal

    stats = daily_job()
    receipt = stats.get("coverage_receipt") or {}
    if not receipt.get("valid_count"):
        stats["factor_refresh"] = {"written": 0, "reason": "target_bars_unverified_or_empty"}
        logger.warning("个股因子日快照跳过：目标日日线为空或无法核验 date=%s", receipt.get("trade_date"))
        return stats

    target = date.fromisoformat(receipt["trade_date"]).strftime("%Y%m%d")
    try:
        with SessionLocal() as db:
            stats["factor_refresh"] = collect_once(db, trade_date=target)
    except Exception as exc:  # noqa: BLE001 - preserve actual upstream result
        stats["factor_refresh"] = {"written": 0, "reason": "collect_failed"}
        logger.error("个股因子日快照计算失败: date=%s error=%s", target, type(exc).__name__)
    return stats
