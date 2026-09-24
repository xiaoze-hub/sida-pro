"""TQ 条件选股信号定时采集(单写者): 每交易日收盘后扫一遍全市场条件选股公式。

照抄 `TqSentimentScheduler` 形状(AsyncIOScheduler + 全局单例 + bootstrap 启动)。

为什么 15:50 而不是跟情绪序列同点(15:35)
------------------------------------
TQ 网关背后是**一个** Windows 客户端进程。全市场条件选股扫描每公式 ~24s
(10 个公式 ≈ 4 分钟), 与情绪序列同步**同时**开跑会把客户端压出假死(实测唯一解是
整机重启)。错开 15 分钟是刻意的。

为什么收盘后
----------
日线条件信号当日值要收盘才定稿(MACD/KDJ 用的是当日收盘价), 盘中取到的是未定稿值。

失败语义: 未开盘/非交易日/TQ 不可用都静默跳过; 单公式失败只记日志, 不抛。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

# 收盘后 15:50 — 与 tq_sentiment(15:35) 错开 15 分钟, 避免两个采集器抢同一个 TQ 客户端
RUN_HOUR = 15
RUN_MINUTE = 50

#: 首次启动(表为空)时的回补交易日数。每天要扫 10 个公式 × 全市场(~80s),
#: 所以刻意保守 —— 回补是**后台线程**, 不阻塞启动。
BACKFILL_TRADING_DAYS = 10
#: 表里少于这个行数(行 = 公式)就认为需要回补
MIN_SAMPLE_ROWS = 10
#: 回补时每天之间歇一下, 让客户端喘口气
BACKFILL_SLEEP_SEC = 2.0

_global_scheduler: "TqFormulaSignalScheduler | None" = None


def _is_market_day() -> bool:
    return datetime.now().weekday() < 5  # 节假日留 hook(取不到就当交易日处理)


def _recent_trading_days(n: int) -> list[str]:
    """最近 n 个"像交易日"的日子(周一~周五倒推)。真实交易日由 TQ 返回值自证。"""
    out: list[str] = []
    day = datetime.now(ZoneInfo("Asia/Shanghai"))
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.strftime("%Y%m%d"))
        day -= timedelta(days=1)
    return out


def _needs_backfill(db) -> bool:
    from sqlalchemy import text

    try:
        n = db.execute(text("SELECT COUNT(*) FROM tq_formula_signal_daily")).scalar() or 0
    except Exception:  # noqa: BLE001 — 表还没建好(迁移未跑)时不回补
        return False
    return int(n) < MIN_SAMPLE_ROWS


def _sync_in_job(*, backfill: bool = False) -> dict:
    import time

    from src.collectors.tq_formula_signals import sync_formula_signals
    from src.db.session import SessionLocal

    if not backfill:
        db = SessionLocal()
        try:
            out = sync_formula_signals(db)
            logger.info("[tq formula] 同步完成: %s", out)
            return out
        finally:
            db.close()

    # 回补: 逐日扫(每天一次全市场, 慢但一次性)
    days = _recent_trading_days(BACKFILL_TRADING_DAYS)
    done: list[str] = []
    for i, day in enumerate(days):
        db = SessionLocal()
        try:
            out = sync_formula_signals(db, trade_date=day)
            if out.get("error"):
                logger.warning("[tq formula] 回补 %s 跳过: %s", day, out["error"])
                continue
            done.append(str(out.get("trade_date") or day))
        except Exception as e:  # noqa: BLE001
            logger.warning("[tq formula] 回补 %s 失败: %s", day, e)
        finally:
            db.close()
        if i < len(days) - 1:
            time.sleep(BACKFILL_SLEEP_SEC)
    logger.info("[tq formula] 回补完成 %d 天: %s", len(done), done)
    return {"backfilled": len(done), "dates": done}


class TqFormulaSignalScheduler:
    """TQ 条件选股信号日序列调度器。"""

    def __init__(self, timezone: str = "Asia/Shanghai"):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self._running = False

    async def _job(self):
        if self._running:
            logger.warning("[tq formula] 上轮还在跑, 跳过本轮")
            return
        if not _is_market_day():
            return
        self._running = True
        try:
            import asyncio

            await asyncio.to_thread(_sync_in_job)
        except Exception as e:  # noqa: BLE001
            logger.error("[tq formula] 定时采集失败: %s", e)
        finally:
            self._running = False

    async def _startup_backfill(self):
        """启动时若表为空则回补(不阻塞启动)。"""
        try:
            import asyncio

            from src.db.session import SessionLocal

            def _check():
                db = SessionLocal()
                try:
                    return _needs_backfill(db)
                finally:
                    db.close()

            if await asyncio.to_thread(_check):
                logger.info("[tq formula] 样本不足, 启动回补 %d 个交易日", BACKFILL_TRADING_DAYS)
                await asyncio.to_thread(_sync_in_job, backfill=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("[tq formula] 启动回补跳过: %s", e)

    def start(self):
        global _global_scheduler
        _global_scheduler = self
        self.scheduler.add_job(
            self._job,
            "cron",
            day_of_week="mon-fri",
            hour=RUN_HOUR,
            minute=RUN_MINUTE,
            id="tq_formula_signal_daily",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._startup_backfill,
            "date",
            run_date=datetime.now(),
            id="tq_formula_signal_boot_backfill",
            replace_existing=True,
        )
        self.scheduler.start()
        try:
            from src.core.scheduler_registry import register

            register("tq_formula_signal", self.scheduler)
        except Exception:  # noqa: BLE001 — 注册表缺失不阻断
            pass
        logger.info("[tq formula] 调度器已启动: 每交易日 %02d:%02d", RUN_HOUR, RUN_MINUTE)

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass

    def trigger_now(self, *, backfill: bool = False) -> dict:
        """手动触发(管理界面/测试用)。"""
        return _sync_in_job(backfill=backfill)
