"""L2 逐笔定期落库调度器(v0.4.77)。

设计:
  - 5 分钟一次, 拉自选股+候选池当日 THS L2 逐笔 → l2_ticks 表
  - 解决"前端每次调 /api/klines/{symbol}/l2-ticks 都 fetch=1 实时拉"导致 30s 超时
  - 默认 fetch=0 后, 后台 cron 是唯一写入路径

复用:
  - src.core.history_store.persist_l2_ticks (已有)
  - src.core.dark_l2.fetch_l2_ticks (thsdk_big_order 源)
  - get_default_symbols() (K线回填同款, 自选股+候选池)
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text

logger = logging.getLogger(__name__)


_global_scheduler: "L2TicksScheduler | None" = None

INTERVAL_MINUTES = 5  # 5min 一次, 盘中 48 次/天
SOURCE = "thsdk_big_order"


def _engine():
    from src.web.database import engine
    return engine


def _symbols_in_worker() -> list[tuple[str, str]]:
    """拉要更新的股票列表(自选股 + 候选池)。"""
    try:
        from src.collectors.klines_ingestor import get_default_symbols
        return list(get_default_symbols() or [])
    except Exception as e:  # noqa: BLE001
        logger.debug("l2 cron 取股票列表失败: %s", e)
        return []


def _one_symbol_fetch(symbol: str, market: str) -> int:
    """拉一只股的 L2 逐笔并落库, 返回写入行数。"""
    try:
        from src.core import dark_l2
        from src.core.history_store import persist_l2_ticks

        # 简化: 直接传 6 位代码, fetch_l2_ticks 内部应处理
        ticks = dark_l2.fetch_l2_ticks(symbol, SOURCE)
        if not ticks:
            return 0
        return persist_l2_ticks(symbol, market, SOURCE, ticks)
    except Exception as e:  # noqa: BLE001
        logger.debug("l2 cron %s fetch failed: %s", symbol, e)
        return 0


def _run_l2_cron() -> dict:
    """线程里跑 L2 批量拉取, 不阻塞 asyncio。"""
    pairs = _symbols_in_worker()
    if not pairs:
        return {"fetched": 0, "written": 0, "symbols": 0}
    # 盘中判断
    from zoneinfo import ZoneInfo
    now_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
    in_trading = (9 <= now_cn.hour < 16) and not (now_cn.hour == 12 and now_cn.minute < 30)
    if not in_trading:
        logger.debug("[l2 cron] 非盘中, 跳过拉取")
        return {"fetched": 0, "written": 0, "symbols": 0, "skipped": "non_trading"}
    total_written = 0
    total_fetched = 0
    start = time.time()
    for sym, mkt in pairs:
        try:
            w = _one_symbol_fetch(sym, mkt)
            total_written += w
            total_fetched += 1
        except Exception as e:  # noqa: BLE001
            logger.debug("l2 cron %s loop err: %s", sym, e)
    elapsed = time.time() - start
    logger.info(
        f"[l2 cron] 完成: symbols={total_fetched}/{len(pairs)} "
        f"written={total_written} rows / {elapsed:.1f}s"
    )
    return {
        "fetched": total_fetched,
        "written": total_written,
        "symbols": len(pairs),
        "elapsed": elapsed,
    }


class L2TicksScheduler:
    """L2 逐笔 5 分钟定期落库调度器。"""

    def __init__(self, tz_name: str = "Asia/Shanghai"):
        self.scheduler = AsyncIOScheduler(timezone=tz_name)
        self._running = False

    async def _cron_job(self):
        if self._running:
            logger.warning("[l2 cron] 上轮还在跑, 跳过")
            return
        self._running = True
        try:
            await asyncio.to_thread(_run_l2_cron)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"[l2 cron] 异常: {e}")
        finally:
            self._running = False

    def start(self):
        global _global_scheduler
        _global_scheduler = self
        self.scheduler.add_job(
            self._cron_job,
            "interval",
            minutes=INTERVAL_MINUTES,
            id="l2_ticks_5min",
            replace_existing=True,
            coalesce=True,  # 错过的多次合并成一次
            max_instances=1,
        )
        self.scheduler.start()
        try:
            from src.core.scheduler_registry import register
            register("l2_ticks_cron", self.scheduler)
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"L2 逐笔落库调度器已启动: 每 {INTERVAL_MINUTES} 分钟")

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("L2 逐笔落库调度器已关闭")
