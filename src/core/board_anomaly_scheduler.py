"""板块异动采样调度器(2026-11-11)。

每 2 分钟采一轮全板块快照, 交易时段外自动跳过(`sample_once` 内部按交易日历 ∩ 交易
时段判定)。收盘后 15:10 清一次过期数据(快照留 3 个交易日, 记录留 20 个交易日)。

为什么不能只靠前端轮询: 用户不开页面就没有异动记录 —— 而"盘中异动"的价值恰恰在
事后回看。与 board_flow_sampler 的 `collect_once` 同动机。
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_global_scheduler: "BoardAnomalyScheduler | None" = None


def _sample_job() -> None:
    try:
        from src.core.board_anomaly import sample_once

        out = sample_once()
        if out.get("ok"):
            logger.debug("[board-anomaly] 采样 %s", out)
        else:
            logger.debug("[board-anomaly] 跳过: %s", out.get("skipped") or out.get("reason"))
    except Exception as exc:  # noqa: BLE001 — 采样失败不能影响其他调度
        logger.warning("[board-anomaly] 采样异常: %s", exc)


def _prune_job() -> None:
    try:
        from src.core.board_anomaly import prune
        from src.db.session import engine

        with engine.begin() as conn:
            out = prune(conn)
        if any(out.values()):
            logger.info("[board-anomaly] 清理完成: %s", out)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[board-anomaly] 清理异常: %s", exc)


class BoardAnomalyScheduler:
    def __init__(self, timezone: str = "Asia/Shanghai", interval_s: int = 120) -> None:
        self.scheduler = BackgroundScheduler(timezone=timezone)
        self.interval_s = int(interval_s)

    def start(self):
        global _global_scheduler
        _global_scheduler = self
        self.scheduler.add_job(
            _sample_job,
            "interval",
            seconds=self.interval_s,
            id="board_anomaly_sampler",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            _prune_job,
            "cron",
            day_of_week="mon-fri",
            hour=15,
            minute=10,
            id="board_anomaly_prune",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()
        try:
            from src.core.scheduler_registry import register

            register("board_anomaly", self.scheduler)
        except Exception:  # noqa: BLE001 — 注册表缺失不阻断
            pass
        logger.info("[board-anomaly] 调度器已启动: 每 %ds 采样(交易时段内生效)", self.interval_s)

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass


def start_scheduler(timezone: str = "Asia/Shanghai", interval_s: int = 120) -> "BoardAnomalyScheduler":
    s = BoardAnomalyScheduler(timezone=timezone, interval_s=interval_s)
    s.start()
    return s
