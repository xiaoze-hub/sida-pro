"""国家队持仓同步调度器（2026-11-18）。

为什么需要调度：国家队持仓是**季报口径**（一年四更：4/8/10 月底与次年 4 月），
上游 END_DATE 会在披露期某天出现。没有调度就只能在用户点「刷新」时才更新，
页面上的「报告期」会永远停在最后一次手动采集的那期。

节奏：每天 19:40（与 TQ 类 15:35/15:50、广度 18:40、决策预热 15:45 错开）检查一次
「上游最新报告期是否比库里的新」，新了才采集最近两期（当期 + 上一期，用于环比）。
检查本身只有 1 次上游请求，日常成本可忽略。

开关：`SIDA_NATIONAL_TEAM_SYNC=0` 不注册调度（默认启用）。
失败不抛出：日志告警，等下一轮；采集失败的 receipt 由 `/api/national-team/refresh` 手动补。
"""
from __future__ import annotations

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_global_scheduler: "NationalTeamScheduler | None" = None


def _sync_job() -> None:
    try:
        from src.core import national_team as nt
        from src.db.session import engine

        periods = nt.discover_report_periods(limit=2)
        latest = nt.latest_period(engine, "float")
        if latest and periods and periods[0] <= latest:
            logger.debug("[national-team] 上游最新 %s ≤ 库内 %s，跳过", periods[0], latest)
            return
        receipt = nt.sync(engine, periods=periods[:2])
        logger.info(
            "[national-team] 同步完成 periods=%s written=%s ok=%s errors=%s",
            receipt.get("periods"),
            receipt.get("written"),
            receipt.get("ok"),
            len(receipt.get("errors") or []),
        )
    except Exception as exc:  # noqa: BLE001 — 调度失败不能影响其他任务
        logger.warning("[national-team] 同步异常: %s", exc)


class NationalTeamScheduler:
    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        self.scheduler = BackgroundScheduler(timezone=timezone)

    def start(self):
        global _global_scheduler
        _global_scheduler = self
        if os.environ.get("SIDA_NATIONAL_TEAM_SYNC", "1") in {"0", "false", "False"}:
            logger.info("[national-team] SIDA_NATIONAL_TEAM_SYNC=0，不注册同步调度")
            return self
        self.scheduler.add_job(
            _sync_job,
            "cron",
            hour=19,
            minute=40,
            id="national_team_sync",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        self.scheduler.start()
        return self

    def stop(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["NationalTeamScheduler"]
