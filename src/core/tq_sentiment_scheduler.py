"""TQ 情绪序列定时同步(单写者): 每交易日收盘后拉一次市场情绪日序列。

照抄 MarketSnapshotScheduler 形状(AsyncIOScheduler + 全局单例 + server lifespan 启动)。

为什么收盘后: SC 序列是**日频**数据, 盘中当日值未定稿(SC3 涨停家数盘中会持续变化),
收盘后取才是当日终值。周末与 TQ 不可用都静默跳过, 失败只记日志不抛。
"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

# 收盘后 15:35 — 留 5 分钟给交易所/客户端定稿
RUN_HOUR = 15
RUN_MINUTE = 35

# 首次启动(表为空/样本太少)时的回补天数(自然日)
BACKFILL_DAYS = 420
# 样本少于这个数就认为需要回补
MIN_SAMPLE_ROWS = 30

_global_scheduler: "TqSentimentScheduler | None" = None


def _is_market_day() -> bool:
    return datetime.now().weekday() < 5  # 周一到周五, 节假日留 hook(取不到就当交易日处理)


def _needs_backfill(db) -> bool:
    from sqlalchemy import text

    try:
        n = db.execute(text("SELECT COUNT(*) FROM market_sentiment_daily")).scalar() or 0
    except Exception:  # noqa: BLE001 — 表还没建好(迁移未跑)时不回补
        return False
    return int(n) < MIN_SAMPLE_ROWS


def _sync_in_job(*, backfill: bool = False) -> dict:
    from src.web.database import SessionLocal
    from src.collectors.tq_sentiment_series import sync_sentiment_series

    db = SessionLocal()
    try:
        out = sync_sentiment_series(db, days=BACKFILL_DAYS if backfill else 30)
        logger.info("[tq sentiment] 同步完成%s: %s", "(回补)" if backfill else "", out)
        return out
    finally:
        db.close()


class TqSentimentScheduler:
    """TQ 市场情绪日序列调度器。"""

    def __init__(self, timezone: str = "Asia/Shanghai"):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self._running = False

    async def _job(self):
        if self._running:
            logger.warning("[tq sentiment] 上轮还在跑, 跳过本轮")
            return
        if not _is_market_day():
            return
        self._running = True
        try:
            import asyncio

            await asyncio.to_thread(_sync_in_job)
        except Exception as e:  # noqa: BLE001
            logger.error("[tq sentiment] 定时同步失败: %s", e)
        finally:
            self._running = False

    async def _startup_backfill(self):
        """启动时若样本不足则回补一次(不阻塞启动)。"""
        try:
            import asyncio

            from src.web.database import SessionLocal

            def _check():
                db = SessionLocal()
                try:
                    return _needs_backfill(db)
                finally:
                    db.close()

            if await asyncio.to_thread(_check):
                logger.info("[tq sentiment] 样本不足, 启动回补 %s 天", BACKFILL_DAYS)
                await asyncio.to_thread(_sync_in_job, backfill=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("[tq sentiment] 启动回补跳过: %s", e)

    def start(self):
        global _global_scheduler
        _global_scheduler = self
        self.scheduler.add_job(
            self._job,
            "cron",
            day_of_week="mon-fri",
            hour=RUN_HOUR,
            minute=RUN_MINUTE,
            id="tq_sentiment_daily",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._startup_backfill,
            "date",
            run_date=datetime.now(),
            id="tq_sentiment_boot_backfill",
            replace_existing=True,
        )
        self.scheduler.start()
        try:
            from src.core.scheduler_registry import register

            register("tq_sentiment", self.scheduler)
        except Exception:  # noqa: BLE001 — 注册表缺失不阻断
            pass
        logger.info(
            "[tq sentiment] 调度器已启动: 每交易日 %02d:%02d", RUN_HOUR, RUN_MINUTE
        )

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass

    def trigger_now(self, *, backfill: bool = False) -> dict:
        """手动触发(管理界面/测试用)。"""
        return _sync_in_job(backfill=backfill)
