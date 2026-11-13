"""上下文维护调度器：后验评估 + 过期数据清理 + 机会自动刷新。"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.collectors.kline_collector import kline_source
from src.core.context_store import cleanup_context_data
from src.core.entry_candidates import evaluate_entry_candidate_outcomes
from src.core.prediction_outcome import evaluate_pending_prediction_outcomes
from src.core.strategy_engine import (
    evaluate_strategy_outcomes,
    refresh_strategy_signals,
)

logger = logging.getLogger(__name__)

#: 自动调权已停用(2026-11-12 独立审计)。
#: 旧行为: 每 6h 后验评估后内部调用 `rebalance_strategy_weights(actor=None)`,
#: 不经任何 HTTP 端点、不留认证身份, 直接改写所有用户共用的全局策略权重 ——
#: 用户要求的「不得静默自动调权」被完全绕过。现在调度器**只**做只读/评估类工作:
#: 权重变更只能由具备 manage_settings 的人走
#: `POST /api/recommendations/strategy-weights/rebalance`(审计记服务端身份)。
STRATEGY_REBALANCE_DISABLED_REASON = (
    "自动调权已停用: 策略权重只能由具备 manage_settings 的认证用户手动触发 "
    "POST /api/recommendations/strategy-weights/rebalance(审计记录身份), "
    "调度器不会自动改写全局权重"
)


class ContextMaintenanceScheduler:
    def __init__(
        self,
        timezone: str = "UTC",
        eval_interval_hours: int = 6,
        snapshot_retention_days: int = 180,
        outcome_retention_days: int = 365,
    ):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self.eval_interval_hours = max(1, int(eval_interval_hours))
        self.snapshot_retention_days = max(30, int(snapshot_retention_days))
        self.outcome_retention_days = max(60, int(outcome_retention_days))
        self._evaluating = False
        self._cleaning = False
        self._refreshing = False

    async def _evaluate_job(self):
        if self._evaluating:
            logger.debug("[上下文维护] 上一轮后验评估仍在执行，跳过本轮")
            return
        self._evaluating = True
        try:
            stats = await asyncio.to_thread(evaluate_pending_prediction_outcomes)
            if stats.get("scoring_blocked"):
                # 评分闸门阻断: 索引缺失/定义错误或存量重复键, 本轮一条都没评估 —— 必须
                # ERROR 级留痕, 否则"评估完成 pending=N evaluated=0"会被误读成"没有到期数据"
                logger.error(
                    "[上下文维护] 后验评估被评分闸门阻断(%s): %s —— 本轮不评估/不回填/不计分"
                    "(欠评估 %s 条)",
                    (stats.get("scoring_status") or {}).get("reason_code"),
                    stats.get("note"),
                    stats.get("total_pending"),
                )
            elif stats.get("scoring_repaired"):
                # 兼容旧结果字段: 运行期不再自动重建索引, 正常不会进入这里。若真的出现,
                # 说明调用链被改回了 DDL 路径, 必须告警核查。
                logger.warning(
                    "[上下文维护] 评估路径报告了索引重建(运行期不应发生, 请核查是否误用 "
                    "allow_repair=True): %s",
                    ", ".join(stats.get("scoring_repaired") or []),
                )
            level = logging.INFO if stats.get("evaluated", 0) else logging.DEBUG
            logger.log(
                level,
                "[上下文维护] 后验评估完成: pending=%s eligible=%s evaluated=%s skipped_not_due=%s skipped_no_price=%s",
                stats.get("total_pending", 0),
                stats.get("eligible", 0),
                stats.get("evaluated", 0),
                stats.get("skipped_not_due", 0),
                stats.get("skipped_no_price", 0),
            )
            with kline_source("outcome_eval"):
                cand_stats = await asyncio.to_thread(
                    evaluate_entry_candidate_outcomes,
                    horizons=(1, 3, 5, 10),
                    snapshot_days=45,
                    limit=5000,
                )
            missing_after = int(cand_stats.get("missing_due_pairs_after", 0))
            level = (
                logging.WARNING
                if missing_after > 0
                else (logging.INFO if cand_stats.get("evaluated", 0) else logging.DEBUG)
            )
            logger.log(
                level,
                "[上下文维护] 候选后验评估完成: total=%s eligible=%s evaluated=%s skipped_not_due=%s skipped_no_price=%s kline_failures=%s 到期未验证缺口=%s",
                cand_stats.get("total_candidates", 0),
                cand_stats.get("eligible", 0),
                cand_stats.get("evaluated", 0),
                cand_stats.get("skipped_not_due", 0),
                cand_stats.get("skipped_no_price", 0),
                cand_stats.get("kline_failures", 0),
                missing_after,
            )
            if missing_after > 0:
                logger.warning(
                    "[上下文维护] 仍有 %s 个 (active候选,horizon) 到期未验证 —— 下轮自动补验; 持续不降请查 K线数据源",
                    missing_after,
                )
            with kline_source("outcome_eval"):
                strategy_stats = await asyncio.to_thread(
                    evaluate_strategy_outcomes,
                    horizons=(1, 3, 5, 10),
                    snapshot_days=60,
                    limit=1200,
                )
            level = logging.INFO if strategy_stats.get("evaluated", 0) else logging.DEBUG
            logger.log(
                level,
                "[上下文维护] 策略后验评估完成: total=%s eligible=%s evaluated=%s skipped_not_due=%s skipped_no_price=%s",
                strategy_stats.get("total_signals", 0),
                strategy_stats.get("eligible", 0),
                strategy_stats.get("evaluated", 0),
                strategy_stats.get("skipped_not_due", 0),
                strategy_stats.get("skipped_no_price", 0),
            )
            # 自动调权已停用: 这里曾经每 6h 以 actor=None 静默改写全局策略权重。
            # 调度器不再拥有任何写权重的入口(见 STRATEGY_REBALANCE_DISABLED_REASON)。
            logger.debug("[上下文维护] %s", STRATEGY_REBALANCE_DISABLED_REASON)

            # Phase 4 → 因子标定:只读产出「建议」,调度器绝不自动改写有效权重;
            # 改权须人工走既有手工 API(manual 审计),本模块无任何写权开关。
            try:
                from src.core.factor_calibration import calibrate_all_markets

                fcal = await asyncio.to_thread(calibrate_all_markets)
                proposed = sum(r.get("proposed", 0) for r in fcal.values())
                errors = {m: r["error"] for m, r in fcal.items() if r.get("error")}
                logger.log(
                    logging.WARNING if errors else (
                        logging.INFO if proposed else logging.DEBUG
                    ),
                    "[上下文维护] 因子标定建议完成(未写权重): proposed=%s detail=%s errors=%s",
                    proposed,
                    {m: r.get("proposed", 0) for m, r in fcal.items()},
                    errors or None,
                )
                # 观测保留在日志里(只读,不落库):每因子 IC 便于事后核查。
                logger.debug(
                    "[上下文维护] 因子标定观测(只读): %s",
                    {
                        m: {
                            p.get("factor_code"): p.get("ic")
                            for p in r.get("proposals", [])
                        }
                        for m, r in fcal.items()
                    },
                )
            except Exception as fc_err:
                logger.warning("[上下文维护] 因子标定建议失败(未写权重): %s", fc_err)
        except Exception as e:
            logger.exception(f"[上下文维护] 后验评估异常: {e}")
        finally:
            self._evaluating = False

    async def _cleanup_job(self):
        if self._cleaning:
            logger.debug("[上下文维护] 上一轮清理仍在执行，跳过本轮")
            return
        self._cleaning = True
        try:
            deleted = await asyncio.to_thread(
                cleanup_context_data,
                snapshot_days=self.snapshot_retention_days,
                topic_days=self.snapshot_retention_days,
                context_run_days=self.snapshot_retention_days,
                outcome_days=self.outcome_retention_days,
            )
            # deleted 是 dict,任一字段 >0 就是有清理动作
            has_work = bool(deleted and any(deleted.values()) if isinstance(deleted, dict) else deleted)
            level = logging.INFO if has_work else logging.DEBUG
            logger.log(level, "[上下文维护] 清理完成: %s", deleted)
        except Exception as e:
            logger.exception(f"[上下文维护] 清理异常: {e}")
        finally:
            self._cleaning = False

    async def evaluate_once(self) -> dict:
        agent_task = asyncio.to_thread(evaluate_pending_prediction_outcomes)
        candidate_task = asyncio.to_thread(
            evaluate_entry_candidate_outcomes,
            horizons=(1, 3, 5, 10),
            snapshot_days=45,
            limit=5000,
        )
        strategy_eval_task = asyncio.to_thread(
            evaluate_strategy_outcomes,
            horizons=(1, 3, 5, 10),
            snapshot_days=60,
            limit=1200,
        )
        agent_stats, candidate_stats, strategy_eval_stats = await asyncio.gather(
            agent_task,
            candidate_task,
            strategy_eval_task,
        )
        # 因子标定建议:须在 outcome 评估之后(IC 才新鲜),不能并进上面的 gather;
        # 只读 —— 只回建议,不改有效权重。
        from src.core.factor_calibration import calibrate_all_markets

        factor_calibration_stats = await asyncio.to_thread(calibrate_all_markets)
        fcal_errors = {
            m: r["error"] for m, r in factor_calibration_stats.items() if r.get("error")
        }
        if fcal_errors:
            logger.warning("[上下文维护] 因子标定建议失败(未写权重): %s", fcal_errors)
        return {
            "agent_predictions": agent_stats,
            "entry_candidates": candidate_stats,
            "strategy_outcomes": strategy_eval_stats,
            # 保持键位兼容, 但明确告诉调用方: 自动调权已停用, changed 恒 0。
            "strategy_rebalance": {
                "disabled": True,
                "changed": 0,
                "reason": STRATEGY_REBALANCE_DISABLED_REASON,
            },
            "factor_calibration": factor_calibration_stats,
        }

    async def _refresh_opportunities_job(self):
        """定时刷新机会池（候选 + 策略信号）。"""
        if self._refreshing:
            logger.debug("[上下文维护] 上一轮机会刷新仍在执行，跳过本轮")
            return
        self._refreshing = True
        try:
            with kline_source("refresh_opportunities"):
                result = await asyncio.to_thread(
                    refresh_strategy_signals,
                    rebuild_candidates=True,
                    max_inputs=500,
                    market_scan_limit=80,
                    max_kline_symbols=60,
                    limit_candidates=2000,
                )
            level = logging.INFO if result.get("count", 0) else logging.DEBUG
            logger.log(
                level,
                "[上下文维护] 机会自动刷新完成: snapshot_date=%s count=%s",
                result.get("snapshot_date", ""),
                result.get("count", 0),
            )
        except Exception as e:
            logger.exception(f"[上下文维护] 机会自动刷新异常: {e}")
        finally:
            self._refreshing = False

    async def refresh_opportunities_once(self) -> dict:
        """手动触发一次机会刷新。"""
        with kline_source("refresh_opportunities"):
            return await asyncio.to_thread(
                refresh_strategy_signals,
                rebuild_candidates=True,
                max_inputs=500,
                market_scan_limit=80,
                max_kline_symbols=60,
                limit_candidates=2000,
            )

    async def cleanup_once(self) -> dict:
        return await asyncio.to_thread(
            cleanup_context_data,
            snapshot_days=self.snapshot_retention_days,
            topic_days=self.snapshot_retention_days,
            context_run_days=self.snapshot_retention_days,
            outcome_days=self.outcome_retention_days,
        )

    def start(self):
        self.scheduler.add_job(
            self._evaluate_job,
            "interval",
            hours=self.eval_interval_hours,
            jitter=120,  # 错峰,避免与 price_alert/paper_trading(60s)同刻写 SQLite
            id="context_maintenance_evaluate",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        self.scheduler.add_job(
            self._cleanup_job,
            "cron",
            hour=4,
            minute=15,
            jitter=120,
            id="context_maintenance_cleanup",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        # 机会自动刷新（北京时间 09:15 / 13:30 / 22:00）
        for job_hour, job_minute in ((1, 15), (5, 30), (14, 0)):
            self.scheduler.add_job(
                self._refresh_opportunities_job,
                "cron",
                hour=job_hour,
                minute=job_minute,
                jitter=120,  # 错峰,避免与其它调度同刻写 SQLite
                id=f"context_maintenance_refresh_opportunities_{job_hour:02d}{job_minute:02d}",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=300,
            )
        self.scheduler.start()
        from src.core.scheduler_registry import register
        register("context", self.scheduler)
        try:
            from src.core.error_tracker import install_scheduler_error_tracking

            install_scheduler_error_tracking(self.scheduler)
        except Exception:
            pass
        logger.info(
            "上下文维护调度器已启动（后验评估间隔 %sh，快照保留 %s 天，后验保留 %s 天，机会自动刷新 01:15/05:30/14:00 UTC）",
            self.eval_interval_hours,
            self.snapshot_retention_days,
            self.outcome_retention_days,
        )

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("上下文维护调度器已关闭")
