# -*- coding: utf-8 -*-
"""全市场日线(qfq)一次性回填(v0.5.87)。

用法(容器内):
  python scripts/klines_fullmarket_backfill.py --days 500 --concurrency 8
  python scripts/klines_fullmarket_backfill.py --days 500 --limit 50   # 小批量试跑

可续跑: 完成集合存 --state 指向的 json; 失败符号不写入, 下轮重试。
取数走既有 klines_ingestor.ingest_symbol(marketdata engine 单链单标签), 不新造取数链。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

sys.path.insert(0, ".")

from src.collectors.klines_ingestor import ingest_symbol  # noqa: E402
from src.collectors.stock_list import get_stock_list  # noqa: E402
from src.core.klines_fullmarket import (  # noqa: E402
    a_share_universe,
    load_state,
    run_backfill,
    save_state,
)
from src.db.session import engine  # noqa: E402
from src.models.market import MarketCode  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("klines_fullmarket_backfill")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=500)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help=">0 时只跑前 N 只(试跑)")
    ap.add_argument("--state", default="data/klines_fullmarket_state.json")
    args = ap.parse_args()

    universe = a_share_universe(get_stock_list())
    if args.limit > 0:
        universe = universe[: args.limit]
    state = load_state(args.state)
    done = set(state["done"])
    logger.info("universe=%d already_done=%d days=%d conc=%d",
                len(universe), len(done), args.days, args.concurrency)

    async def ingest(symbol: str, days: int) -> dict:
        return await ingest_symbol(engine, symbol, MarketCode.CN, "1d", days)

    last_save = {"n": 0}

    def on_progress(completed: int, total: int) -> None:
        if completed % 200 == 0:
            logger.info("progress %d/%d", completed, total)
        if completed - last_save["n"] >= 500:
            save_state(args.state, sorted(done))
            last_save["n"] = completed

    t0 = time.time()
    stats = await run_backfill(
        universe, days=args.days, ingest=ingest,
        concurrency=args.concurrency, done=done, on_progress=on_progress,
    )
    save_state(args.state, sorted(done))
    logger.info("done in %.1fs ok=%d fail=%d failed_sample=%s",
                time.time() - t0, stats["ok"], stats["fail"], stats["failed_symbols"][:10])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
