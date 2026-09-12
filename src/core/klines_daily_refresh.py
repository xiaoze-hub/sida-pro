# -*- coding: utf-8 -*-
"""全市场日线(qfq)每日增量 job(v0.5.87)。

交易日 16:00 由 startup 调度: 对全 A 股跑 days=10 的增量 upsert(ON CONFLICT 自愈),
保持连板天梯/逐股日K 的全市场覆盖新鲜。取数走既有 ingest_symbol, 不新造取数链。
同步入口供 APScheduler 线程池调用(asyncio.run 于线程内)。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def daily_job(days: int = 10, concurrency: int = 8) -> dict:
    from src.collectors.klines_ingestor import ingest_symbol
    from src.collectors.stock_list import get_stock_list
    from src.core.klines_fullmarket import a_share_universe, run_backfill
    from src.db.session import engine
    from src.models.market import MarketCode

    universe = a_share_universe(get_stock_list())

    async def ingest(symbol: str, d: int) -> dict:
        return await ingest_symbol(engine, symbol, MarketCode.CN, "1d", d)

    stats = asyncio.run(
        run_backfill(universe, days=days, ingest=ingest, concurrency=concurrency)
    )
    logger.info("全市场日线增量完成: universe=%d ok=%d fail=%d",
                len(universe), stats["ok"], stats["fail"])
    return stats
