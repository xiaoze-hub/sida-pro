"""国家队 / 机构持仓同步调度器（2026-11-18；同日加入社保与险资后重构）。

为什么需要调度：持仓是**季报口径**（一年四更），上游 `END_DATE` 会在披露期某天出现。
没有调度就只能靠人点「采集最新」，页面上的报告期会永远停在最后一次手动采集那期。

两条节奏（成本差一个数量级）：

- **新报告期出现 → 全市场扫描**（`mode="scan"`）：一期两表各约 5.6 万行，3-6 分钟。
  只有扫描才能认出「全国社保基金各组合」「险资各产品账户」这些事先不可枚举的名字，
  结果落进 `national_team_holder_registry`；
- **其余日子 → 定向刷新**（`mode="targeted"`）：按注册表名单 `in (...)` 查最近一期，
  秒级，足以吸收披露期内的补正/修订。

开关：`SIDA_NATIONAL_TEAM_SYNC=0` 不注册调度（默认启用）。
失败不抛出：日志告警，下一轮再试；手动补数走 `/api/national-team/refresh`。
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
        registry_empty = not nt.registry_names(engine)

        if not latest or registry_empty or (periods and periods[0] > latest):
            # 首次 / 名单缺失 / 新报告期：必须全市场扫描，才能认全社保与险资账户
            rc = nt.sync(engine, periods=periods[:2], mode="scan")
            logger.info(
                "[national-team] 扫描同步(periods=%s) ok=%s written=%s errors=%s groups=%s",
                rc.get("periods"), rc.get("ok"), rc.get("written"),
                len(rc.get("errors") or []), rc.get("groups"),
            )
        else:
            rc = nt.sync(engine, periods=periods[:1], mode="targeted")
            logger.debug("[national-team] 定向刷新 ok=%s written=%s", rc.get("ok"), rc.get("written"))
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
