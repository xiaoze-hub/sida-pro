import asyncio
import json
import logging
from functools import partial
import time
from typing import Any, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.agents.base import BaseAgent, AgentContext
from src.collectors.kline_collector import kline_source
from src.core.agent_runs import record_agent_run
from src.core.log_context import log_context
from src.models.market import MARKETS
from src.core.schedule_parser import parse_schedule

logger = logging.getLogger(__name__)


class AgentScheduler:
    """Agent 调度器"""

    def __init__(self, timezone: str = "UTC"):
        self.scheduler = AsyncIOScheduler()
        self.agents: dict[str, BaseAgent] = {}
        self.execution_modes: dict[str, str] = {}
        self.timezone = timezone
        # 改为存储 context 构建函数，而非固定 context
        self.context_builder: Callable[[str], AgentContext] | None = None
        # M7(2026-09-10): 用户桶解析器(见 set_user_bucket_resolver)
        self.user_bucket_resolver: Callable[[str], list[str | None]] | None = None

    def set_context_builder(self, builder: Callable[[str], AgentContext]):
        """设置 context 构建函数（每次执行时动态构建）"""
        self.context_builder = builder

    def set_user_bucket_resolver(self, resolver: Callable[[str], list[str | None]]):
        """M7(2026-09-10): 提供"某 agent 应按哪些用户分别执行"的解析器。

        返回 user_id 列表(None = 归属为空的遗留共享桶); 空列表 = 不做用户拆分
        (退回单次全量执行, 兼容无 stock_agents 绑定的 agent)。
        """
        self.user_bucket_resolver = resolver

    def register(self, agent: BaseAgent, schedule: str, execution_mode: str = "batch"):
        """
        注册 Agent 到调度器。

        Args:
            agent: Agent 实例
            schedule: 调度表达式
                - cron 格式: "分 时 日 月 周" (5 部分)
                - interval 格式: "interval:3m" 或 "interval:30s"
            execution_mode: 执行模式 batch/single（single 将逐只股票执行 run_single）
        """
        self.agents[agent.name] = agent
        self.execution_modes[agent.name] = execution_mode or "batch"

        # 解析调度表达式
        # cron 使用 5 段: "分 时 日 月 周"
        # 其中 day_of_week 的数字按 POSIX cron 语义(1-5=周一到周五)，会在内部做一次归一化。
        trigger = parse_schedule(schedule, timezone=self.timezone)

        self.scheduler.add_job(
            self._run_agent,
            trigger=trigger,
            args=[agent.name],
            id=agent.name,
            name=agent.display_name,
            replace_existing=True,
            # 2026-09-08 审计修复: LLM Agent 单次动辄数分钟, interval 任务上一轮
            # 没跑完下一轮就启动 → 同一 Agent 并发双跑(重复通知/token 翻倍/记录竞态)。
            # max_instances=1 防并发, coalesce 合并积压, misfire 给 5 分钟宽限。
            # 对齐 server.py 后注册的 4 个 job 与 report/kline_backfill scheduler 的既有口径。
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )

        logger.info(f"注册 Agent: {agent.display_name} (schedule: {schedule})")

    # NOTE: cron/interval 解析逻辑统一放在 src/core/schedule_parser.py

    def _build_contexts(self, agent_name: str) -> list[AgentContext]:
        """构建本次运行要执行的 context 列表。

        M7(2026-09-10): 有 user_bucket_resolver 时按用户桶逐个构建(空自选的桶跳过);
        无解析器/无绑定绑定用户时退回单次全量 context(兼容旧行为)。
        """
        resolver = self.user_bucket_resolver
        buckets = resolver(agent_name) if resolver else []
        if not buckets:
            return [self.context_builder(agent_name)]  # type: ignore[misc]
        contexts: list[AgentContext] = []
        for uid in buckets:
            try:
                ctx = self.context_builder(agent_name, uid)  # type: ignore[misc]
            except TypeError:
                # 旧签名(builder 只吃 agent_name) → 退回全量
                ctx = self.context_builder(agent_name)  # type: ignore[misc]
            if ctx.watchlist:
                contexts.append(ctx)
        return contexts

    async def _run_agent(self, agent_name: str):
        """执行指定 Agent（动态构建 context）"""
        if not self.context_builder:
            logger.error("context_builder 未设置")
            return

        agent = self.agents.get(agent_name)
        if not agent:
            logger.error(f"Agent 未找到: {agent_name}")
            return

        start = time.monotonic()
        trace_id = f"sch-{agent_name}-{int(time.time() * 1000)}"
        try:
            with log_context(
                trace_id=trace_id,
                run_id=trace_id,
                agent_name=agent_name,
                event="agent_run",
                tags={"trigger_source": "schedule"},
            ):
                # 每次执行时动态构建 context（获取最新配置）
                # M7(2026-09-10 多用户隔离): 按用户桶拆分, 每桶单独构建 context(自选/持仓/
                # 通知渠道收敛到该用户), 避免把多个用户的自选混成一份 prompt 且落库为共享行。
                contexts = self._build_contexts(agent_name)
                if not contexts:
                    logger.info(
                        f"[调度] {agent.display_name} 无绑定标的/无用户桶, 跳过本次执行"
                    )
                    return
                context = contexts[0]
                # 风险方案1.3/A3 观测面: 记录本次运行上下文规模(自选+持仓序列化估算)
                try:
                    context_chars = sum(
                        len(json.dumps(
                            {"watchlist": [getattr(s, "symbol", str(s)) for s in c.watchlist],
                             "portfolio": str(c.portfolio)},
                            ensure_ascii=False, default=str))
                        for c in contexts
                    )
                except Exception:
                    context_chars = 0
                logger.info(
                    f"[调度] 开始执行 Agent: {agent.display_name}（用户桶 {len(contexts)}）"
                )
                mode = self.execution_modes.get(agent_name, "batch")
                if mode == "single" and hasattr(agent, "run_single"):
                    processed = 0
                    skipped = 0
                    total_stocks = 0
                    errors: list[str] = []
                    for context in contexts:
                        for stock in list(context.watchlist):
                            total_stocks += 1
                            market_def = MARKETS.get(stock.market)
                            if market_def and not market_def.is_trading_time():
                                skipped += 1
                                logger.info(
                                    f"[调度] 跳过 {agent.display_name} {stock.symbol}（{market_def.name} 非交易时段）"
                                )
                                continue
                            try:
                                with kline_source(f"agent:{agent_name}"):
                                    res = await agent.run_single(context, stock.symbol)  # type: ignore[attr-defined]
                                processed += 1
                                try:
                                    notify_error = (
                                        (res.raw_data or {}).get("notify_error")
                                        if res
                                        else ""
                                    )
                                except Exception:
                                    notify_error = ""
                                if notify_error:
                                    errors.append(f"{stock.symbol} notify: {notify_error}")
                            except Exception as e:
                                logger.error(
                                    f"Agent [{agent_name}] 单只执行失败 {stock.symbol}: {e}",
                                    exc_info=True,
                                )
                                errors.append(f"{stock.symbol}: {e}")
                    logger.info(
                        f"[调度] Agent 单只模式执行完成: {agent.display_name}（用户桶{len(contexts)} 执行{processed}，跳过{skipped}，共{total_stocks}）"
                    )
                    duration_ms = int((time.monotonic() - start) * 1000)
                    # P2-9: 同步 DB 写放线程池, 不卡调度 loop
                    await asyncio.to_thread(
                        partial(
                        record_agent_run,
                        agent_name=agent_name,
                        status="failed" if errors else "success",
                        result=f"single mode executed {processed}, skipped {skipped}, total {total_stocks}, buckets {len(contexts)}",
                        error="; ".join(errors)[:2000],
                        duration_ms=duration_ms,
                        trace_id=trace_id,
                        trigger_source="schedule",
                        context_chars=context_chars,
                        model_label=context.model_label,
                    ))
                else:
                    # M7(2026-09-10): 逐用户桶执行。每个用户的报告只发到其本人渠道,
                    # 故订阅推送跳过"本轮已按用户投递"的人, 避免同一份报告发两遍。
                    results: list[tuple[AgentContext, Any]] = []
                    delivered_users: set[str] = set()
                    for context in contexts:
                        with kline_source(f"agent:{agent_name}"):
                            result = await agent.run(context)
                        results.append((context, result))
                        uid = getattr(getattr(context, "user", None), "id", None)
                        if uid:
                            delivered_users.add(uid)
                    duration_ms = int((time.monotonic() - start) * 1000)
                    last_context, last_result = results[-1]
                    raw = last_result.raw_data or {}
                    notify_errors: list[str] = []
                    for _c, _r in results:
                        try:
                            _e = (_r.raw_data or {}).get("notify_error") or ""
                        except Exception:
                            _e = ""
                        if _e:
                            notify_errors.append(_e)
                    logger.info(
                        f"[调度] Agent 批量模式执行完成: {agent.display_name}（用户桶{len(results)}）"
                    )
                    # P2-9: 同步 DB 写放线程池
                    await asyncio.to_thread(
                        partial(
                        record_agent_run,
                        agent_name=agent_name,
                        status="failed" if notify_errors else "success",
                        result=(last_result.content or "")[:2000],
                        error=("; ".join(notify_errors))[:2000],
                        duration_ms=duration_ms,
                        trace_id=trace_id,
                        trigger_source="schedule",
                        context_chars=context_chars,
                        notify_attempted=(
                            "notified" in raw
                            or "notify_error" in raw
                            or "notify_skipped" in raw
                        ),
                        notify_sent=bool(raw.get("notified", False)),
                        model_label=last_context.model_label,
                    ))
                    # 多用户定时报告推送(2026-08-10 阶段4):
                    # agent 已按用户桶投递本人渠道, 这里只补推"未按用户投递"的订阅者,
                    # 避免同一份报告重复外发(2026-09-10 M7 修正)。
                    try:
                        await asyncio.to_thread(
                            self._push_to_subscribers,
                            agent_name,
                            last_result,
                            delivered_users,
                        )
                    except Exception as e:
                        logger.warning(f"[调度] 订阅推送失败: {e}")
                logger.info(f"[调度] Agent 执行完成: {agent.display_name}")
        except Exception as e:
            logger.error(f"Agent [{agent_name}] 调度执行异常: {e}", exc_info=True)
            # 风险方案1.3/A3: 调度器异常进可观测面(error_tracker JSONL + 聚合告警)
            try:
                from src.core.error_tracker import capture_exception
                capture_exception(e, {"source": "scheduler", "agent": agent_name})
            except Exception:
                pass
            duration_ms = int((time.monotonic() - start) * 1000)
            # P2-9: 同步 DB 写放线程池
            await asyncio.to_thread(
                partial(
                record_agent_run,
                agent_name=agent_name,
                status="failed",
                error=str(e),
                duration_ms=duration_ms,
                trace_id=trace_id,
                trigger_source="schedule",
            ))

    async def trigger_now(self, agent_name: str):
        """立即执行某个 Agent（手动触发）"""
        await self._run_agent(agent_name)

    def _push_to_subscribers(
        self, agent_name: str, result, skip_user_ids: set[str] | None = None
    ) -> None:
        """按订阅用户推送定时报告(2026-08-10 阶段4)。

        报告类型映射: premarket_outlook→premarket, intraday_monitor→intraday,
        afterhours/daily_report→review, prediction→prediction。
        只推订阅了该类型且配置了个人渠道的用户。

        M7(2026-09-10): `skip_user_ids` = 本轮已按用户桶投递过的人(报告已发到其本人
        渠道), 跳过以免同一份报告重复外发。
        """
        report_type = {
            "premarket_outlook": "premarket",
            "intraday_monitor": "intraday",
            "afterhours_review": "review",
            "daily_report": "review",
        }.get(agent_name)
        if not report_type:
            return
        content = getattr(result, "notify_content", None) or getattr(result, "content", "") or ""
        title = getattr(result, "title", "") or agent_name
        if not content:
            return
        try:
            from src.db.session import SessionLocal
            from src.db.models import ReportSubscription, User
            from src.core.notify_center import push_notification

            db = SessionLocal()
            try:
                subs = db.query(ReportSubscription).filter(
                    ReportSubscription.report_type == report_type,
                    ReportSubscription.enabled.is_(True),
                ).all()
                users = {u.id: u for u in db.query(User).filter(User.is_active.is_(True)).all()}
            finally:
                db.close()
            skip = skip_user_ids or set()
            for sub in subs:
                if sub.user_id not in users:
                    continue
                if sub.user_id in skip:
                    logger.info(
                        f"[调度] 订阅推送 {report_type} → 用户 {sub.user_id[:8]} 已在用户桶投递, 跳过"
                    )
                    continue
                try:
                    push_notification(
                        title, content,
                        category="report",
                        level="info",
                        user_id=sub.user_id,
                    )
                    logger.info(f"[调度] 订阅推送 {report_type} → 用户 {sub.user_id[:8]}")
                except Exception as e:
                    logger.warning(f"[调度] 订阅推送用户 {sub.user_id[:8]} 失败: {e}")
        except Exception as e:
            logger.warning(f"[调度] 订阅查询失败: {e}")

    def start(self):
        """启动调度器"""
        self.scheduler.start()
        from src.core.scheduler_registry import register
        register("agent", self.scheduler)
        # 风险方案1.3/A3: job 异常/错过进可观测面(与其余调度器同机制)
        try:
            from src.core.error_tracker import install_scheduler_error_tracking
            install_scheduler_error_tracking(self.scheduler)
        except Exception as e:
            logger.warning(f"[调度] 错误监听安装失败: {e}")
        logger.info(f"调度器已启动，已注册 {len(self.agents)} 个 Agent")

        # 打印所有已注册的任务
        jobs = self.scheduler.get_jobs()
        for job in jobs:
            logger.info(f"  - {job.name}: 下次执行 {job.next_run_time}")

    def shutdown(self):
        """关闭调度器"""
        self.scheduler.shutdown()
        logger.info("调度器已关闭")
