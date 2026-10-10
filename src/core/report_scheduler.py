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

# 报告类型 → 推送文案前缀
_REPORT_LABELS = {"premarket": "盘前", "postmarket": "盘后"}


def _push_report_notification(report_type: str, result: dict) -> bool:
    """生成成功后把内置报告推到通知中心(站内落库 + 外发渠道), 失败/无渠道显式不假装。

    复用 `notify_center.push_notification`(与 agent 推送同一条链: 先站内落库再外发,
    绝不抛异常)。返回值语义:
      - True : 站内通知已落库(外发结果由通知中心的 push_status 记录, 无渠道时
               push_status='skipped', 站内仍可见 —— 不是"沉默失败");
      - False: 站内落库失败(未外发) 或调用异常 —— 调用方须如实记 error, 绝不报成功。

    只落盘不推送是本轮审计的 P1: 报告文件生成后无人知晓, 这里补上"生成即通知"。
    """
    title = str(result.get("title") or "").lstrip("# ").strip() or "SIDA 报告"
    path = str(result.get("path") or "")
    size = result.get("size") or 0
    label = _REPORT_LABELS.get(report_type, report_type)
    body = f"SIDA {label}报告已生成\n标题: {title}\n大小: {size} 字节\n文件: {path}"
    try:
        from src.core.notify_center import push_notification

        nid = push_notification(
            title=title,
            body=body,
            category="report",
            level="info",
            link="/reports",
            source="report_scheduler",
        )
    except Exception as e:  # noqa: BLE001
        logger.error("[报告] %s 推送通知异常(未假装成功): %s", report_type, e)
        return False
    if nid is None:
        # push_notification 返回 None = 站内落库失败 → 未外发, 不假装
        logger.error(
            "[报告] %s 通知站内落库失败(未外发), 报告本体已写入 %s", report_type, path
        )
        return False
    logger.info(
        "[报告] %s 已推送通知中心 id=%s (无渠道时站内存留 push_status=skipped)",
        report_type,
        nid,
    )
    return True


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
            # 盘后报告前先跑全市场三榜扫描落库(设计稿 §6.1, 2026-09-01 接线)。
            # 独立 try 包裹: 三榜扫描失败不影响报告生成本身。
            if report_type == "postmarket":
                try:
                    from src.core.market_scan_jobs import run_market_scan_job

                    scan_res = await asyncio.to_thread(run_market_scan_job)
                    logger.info(
                        "[报告] 盘后三榜扫描: %s",
                        scan_res if scan_res.get("ok") else scan_res.get("error", "跳过"),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception("[报告] 盘后三榜扫描异常: %s", e)

                # 三榜落库后, 聚合 5 块信号 → 渲染 text → 落库 signal_summary_daily
                # (设计稿 §7.3「被动注入」, 2026-09-01 接线)。依赖三榜快照, 故在其后。
                try:
                    from src.core.signal_summary import run_signal_summary_job

                    summary_res = await asyncio.to_thread(run_signal_summary_job)
                    logger.info(
                        "[报告] 系统信号摘要: %s",
                        summary_res if summary_res.get("ok") else summary_res.get("error", "跳过"),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception("[报告] 系统信号摘要异常: %s", e)

                # 全市场暗盘资金 TOP 扫描(设计稿 §6.1 A6, 2026-09-01 接线)。
                # thsdk DDE 批量主力资金流, 全市场约 16s; 依赖 thsdk 登录态。
                try:
                    from src.core.market_scan_jobs import run_dark_fund_top_job

                    dft_res = await asyncio.to_thread(run_dark_fund_top_job)
                    logger.info(
                        "[报告] 暗盘资金TOP: %s",
                        dft_res if dft_res.get("ok") else dft_res.get("error", "跳过"),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception("[报告] 暗盘资金TOP异常: %s", e)

                # 主力资金战报(规格 §4.4, 2026-10-10): 当日主力动向汇总(TOP/BOTTOM +
                # 行业分布 SUPAMO + 个股净额变化 + 拆单/对倒计数)落 war_report_daily。
                # 依赖全市场 DDE(与暗盘 TOP 同链但独立 try, 互不影响)。
                try:
                    from src.core.war_report import run_war_report_job

                    wr_res = await asyncio.to_thread(run_war_report_job)
                    logger.info(
                        "[报告] 主力资金战报: %s",
                        wr_res if wr_res.get("ok") else wr_res.get("reason", "跳过"),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception("[报告] 主力资金战报异常: %s", e)

            result = await asyncio.to_thread(
                _generate_once_in_worker, report_type
            )
            logger.info(
                "[报告] %s 生成完成: %s (%d bytes)",
                report_type,
                result.get("path", ""),
                result.get("size", 0),
            )
            # AI 链路 P1(2026-10-10): 生成成功后推送通知中心(站内 + 外发), 此前只落盘无人知。
            # 失败/无渠道由 _push_report_notification 显式记 error, 不假装推送成功。
            _push_report_notification(report_type, result)
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
            misfire_grace_time=300,
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
            misfire_grace_time=300,
        )
        self.scheduler.start()
        from src.core.scheduler_registry import register

        register("report", self.scheduler)
        try:
            from src.core.error_tracker import install_scheduler_error_tracking

            install_scheduler_error_tracking(self.scheduler)
        except Exception:
            pass
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
