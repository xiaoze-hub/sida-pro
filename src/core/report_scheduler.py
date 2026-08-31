"""SIDA 内置报告调度器: 交易日(周一至五) 8:30 盘前报告 / 15:30 盘后报告。

独立于 Agent 调度, 参考 price_alert_scheduler.py 模式:
- APScheduler cron 触发, 时区取 Settings.app_timezone(默认 Asia/Shanghai)
- 任务在 worker 线程内跑完整生成(asyncio.run), 避免同步数据采集阻塞事件循环
- 失败只记日志不崩, 下次触发继续
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

# 默认触发时刻(本地时区)
PREMARKET_CRON = {"hour": 8, "minute": 30}
POSTMARKET_CRON = {"hour": 15, "minute": 30}


def _generate_once_in_worker(report_type: str) -> dict:
    """在线程内运行完整报告生成(asyncio.run), 内部自开 DB session。"""
    from src.core.report_generator import generate_market_report

    return asyncio.run(generate_market_report(report_type))


class ReportScheduler:
    def __init__(self, timezone: str = "Asia/Shanghai"):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self._running: set[str] = set()

    async def _generate_job(self, report_type: str):
        if report_type in self._running:
            logger.debug("[报告] 上轮 %s 生成仍在执行, 跳过本轮", report_type)
            return
        self._running.add(report_type)
        try:
            result = await asyncio.to_thread(
                _generate_once_in_worker, report_type
            )
            logger.info(
                "[报告] %s 生成完成: %s (%d bytes)",
                report_type,
                result.get("path", ""),
                result.get("size", 0),
            )
        except Exception as e:
            logger.exception("[报告] %s 生成异常: %s", report_type, e)
        finally:
            self._running.discard(report_type)

    def start(self):
        self.scheduler.add_job(
            self._generate_job,
            "cron",
            day_of_week="mon-fri",
            hour=PREMARKET_CRON["hour"],
            minute=PREMARKET_CRON["minute"],
            args=["premarket"],
            id="report_premarket_daily",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self._generate_job,
            "cron",
            day_of_week="mon-fri",
            hour=POSTMARKET_CRON["hour"],
            minute=POSTMARKET_CRON["minute"],
            args=["postmarket"],
            id="report_postmarket_review",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()
        from src.core.scheduler_registry import register

        register("report", self.scheduler)
        logger.info(
            "SIDA 报告调度器已启动: 盘前 %02d:%02d / 盘后 %02d:%02d (周一至五, %s)",
            PREMARKET_CRON["hour"], PREMARKET_CRON["minute"],
            POSTMARKET_CRON["hour"], POSTMARKET_CRON["minute"],
            self.scheduler.timezone,
        )

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("SIDA 报告调度器已关闭")
