"""市场广度日序列调度器 —— 每交易日盘后同步一次（含启动回补）。

为什么要调度: `market_breadth_daily` 由 `sync_breadth_series` 维护；不跑调度，
序列就不会增长，分位与情绪温度会一直停在最后一次同步的日期上。

时间选择: 与 `tq_formula_signal_scheduler` 错开（后者在盘后扫 108 个公式，较重）。
"""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

RUN_HOUR = 18
RUN_MINUTE = 40
MIN_SAMPLE_ROWS = 20          # 少于这个行数视为"还没历史"，启动时回补一次

_global_scheduler: "MarketBreadthScheduler | None" = None


def _sync_in_job(*, days: int = 260) -> dict:
    from src.collectors.market_breadth_series import sync_breadth_series
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        out = sync_breadth_series(db, days=days)
        logger.info("[breadth] 同步完成: %s", out)
        return out
    finally:
        db.close()


def _needs_backfill(db) -> bool:
    from sqlalchemy import text

    try:
        n = db.execute(text("SELECT COUNT(*) FROM market_breadth_daily")).scalar() or 0
    except Exception:  # noqa: BLE001 — 表未建(迁移未跑)时不回补
        return False
    return int(n) < MIN_SAMPLE_ROWS


class MarketBreadthScheduler:
    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        self.scheduler = BackgroundScheduler(timezone=timezone)

    def _job(self):
        try:
            _sync_in_job()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[breadth] 同步失败: %s", exc)

    def _startup_backfill(self):
        from src.db.session import SessionLocal

        db = SessionLocal()
        try:
            if not _needs_backfill(db):
                return
        finally:
            db.close()
        self._job()

    def start(self):
        global _global_scheduler
        _global_scheduler = self
        self.scheduler.add_job(
            self._job, "cron", day_of_week="mon-fri", hour=RUN_HOUR, minute=RUN_MINUTE,
            id="market_breadth_daily", replace_existing=True, coalesce=True, max_instances=1,
        )
        self.scheduler.add_job(
            self._startup_backfill, "date", run_date=datetime.now(),
            id="market_breadth_boot_backfill", replace_existing=True,
        )
        self.scheduler.start()
        try:
            from src.core.scheduler_registry import register

            register("market_breadth", self.scheduler)
        except Exception:  # noqa: BLE001 — 注册表缺失不阻断
            pass
        logger.info("[breadth] 调度器已启动: 每交易日 %02d:%02d", RUN_HOUR, RUN_MINUTE)

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass


def start_scheduler(timezone: str = "Asia/Shanghai") -> "MarketBreadthScheduler":
    s = MarketBreadthScheduler(timezone=timezone)
    s.start()
    return s
