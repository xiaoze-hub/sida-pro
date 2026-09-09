"""运行时核心: 调度器全局实例 + Agent 触发/上下文构建。

原 server.py 原样搬移(W3.2/D1)。调度器全局(scheduler/price_alert_scheduler/...)
是运行期被 lifespan/reload_scheduler 写入的可变状态, server.py shim 以 PEP 562
转发本模块属性, 保证 health 深检/price_alerts 等读到 live 值。
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.agents.base import AgentContext, AccountInfo, PortfolioInfo, PositionInfo
from src.bootstrap.agents import AGENT_REGISTRY
from src.config import AppConfig, Settings, StockConfig
from src.core.agent_catalog import AGENT_KIND_WORKFLOW
from src.core.agent_runs import record_agent_run
from src.core.ai_client import AIClient
from src.core.log_context import log_context
from src.core.context_scheduler import ContextMaintenanceScheduler
from src.core.kline_backfill_scheduler import KlineBackfillScheduler
from src.core.notifier import NotifierManager
from src.core.price_alert_scheduler import PriceAlertScheduler
from src.core.report_scheduler import ReportScheduler
from src.core.scheduler import AgentScheduler
from src.models.market import MarketCode
from src.web.database import SessionLocal
from src.web.models import (
    AgentConfig,
    AIService,
    AIModel,
    AppSettings,
    NotifyChannel,
    Stock,
    StockAgent,
)

logger = logging.getLogger("server")


# 全局 scheduler 实例，供 agents API 调用
scheduler: AgentScheduler | None = None
price_alert_scheduler: PriceAlertScheduler | None = None
paper_trading_scheduler: PaperTradingScheduler | None = None
context_maintenance_scheduler: ContextMaintenanceScheduler | None = None
report_scheduler: ReportScheduler | None = None
kline_backfill_scheduler: KlineBackfillScheduler | None = None

# 2026-08-17 加股 60s 快速 backfill:
# APScheduler 跑在它自己的后台线程(没 asyncio loop),
# 但 server.py 跑在 uvicorn 的 asyncio loop 里,
# 需要把 loop 暴露给 kline_backfill_scheduler 用于跨线程调度。
_kline_oneoff_loop: asyncio.AbstractEventLoop | None = None


def load_watchlist_for_agent(agent_name: str) -> list[StockConfig]:
    """从数据库加载某个 Agent 关联的自选股"""
    db = SessionLocal()
    try:
        stock_agents = (
            db.query(StockAgent).filter(StockAgent.agent_name == agent_name).all()
        )
        stock_ids = [sa.stock_id for sa in stock_agents]
        if not stock_ids:
            return []

        # 绑定优先：只要绑定了 Agent，就纳入执行范围
        stocks = db.query(Stock).filter(Stock.id.in_(stock_ids)).all()
        result = []
        for s in stocks:
            try:
                market = MarketCode(s.market)
            except ValueError:
                market = MarketCode.CN
            result.append(
                StockConfig(
                    symbol=s.symbol,
                    name=s.name,
                    market=market,
                )
            )
        return result
    finally:
        db.close()


def load_portfolio_for_agent(agent_name: str) -> PortfolioInfo:
    """从数据库加载某个 Agent 关联股票的持仓信息（包括多账户）"""
    from src.web.models import Account, Position

    db = SessionLocal()
    try:
        # 获取 Agent 关联的股票 ID
        stock_agents = (
            db.query(StockAgent).filter(StockAgent.agent_name == agent_name).all()
        )
        stock_ids = set(sa.stock_id for sa in stock_agents)
        if not stock_ids:
            return PortfolioInfo()

        # 获取所有启用的账户
        accounts = db.query(Account).filter(Account.enabled == True).all()

        account_infos = []
        for acc in accounts:
            # 获取该账户中属于关联股票的持仓
            positions = (
                db.query(Position)
                .filter(
                    Position.account_id == acc.id,
                    Position.stock_id.in_(stock_ids),
                )
                .all()
            )

            position_infos = []
            for pos in positions:
                stock = pos.stock
                if not stock:
                    continue
                try:
                    market = MarketCode(stock.market)
                except ValueError:
                    market = MarketCode.CN

                position_infos.append(
                    PositionInfo(
                        account_id=acc.id,
                        account_name=acc.name,
                        stock_id=stock.id,
                        symbol=stock.symbol,
                        name=stock.name,
                        market=market,
                        cost_price=pos.cost_price,
                        quantity=pos.quantity,
                        invested_amount=pos.invested_amount,
                        trading_style=pos.trading_style or "swing",
                    )
                )

            account_infos.append(
                AccountInfo(
                    id=acc.id,
                    name=acc.name,
                    available_funds=acc.available_funds,
                    positions=position_infos,
                )
            )

        return PortfolioInfo(accounts=account_infos)
    finally:
        db.close()


def load_portfolio_for_stock(stock_id: int) -> PortfolioInfo:
    """从数据库加载单只股票的持仓信息"""
    from src.web.models import Account, Position

    db = SessionLocal()
    try:
        stock = db.query(Stock).filter(Stock.id == stock_id).first()
        if not stock:
            return PortfolioInfo()

        try:
            market = MarketCode(stock.market)
        except ValueError:
            market = MarketCode.CN

        accounts = db.query(Account).filter(Account.enabled == True).all()

        account_infos = []
        for acc in accounts:
            pos = (
                db.query(Position)
                .filter(
                    Position.account_id == acc.id,
                    Position.stock_id == stock_id,
                )
                .first()
            )

            position_infos = []
            if pos:
                position_infos.append(
                    PositionInfo(
                        account_id=acc.id,
                        account_name=acc.name,
                        stock_id=stock.id,
                        symbol=stock.symbol,
                        name=stock.name,
                        market=market,
                        cost_price=pos.cost_price,
                        quantity=pos.quantity,
                        invested_amount=pos.invested_amount,
                        trading_style=pos.trading_style or "swing",
                    )
                )

            account_infos.append(
                AccountInfo(
                    id=acc.id,
                    name=acc.name,
                    available_funds=acc.available_funds,
                    positions=position_infos,
                )
            )

        return PortfolioInfo(accounts=account_infos)
    finally:
        db.close()


def _get_proxy() -> str:
    """从 app_settings 获取 http_proxy"""
    db = SessionLocal()
    try:
        setting = db.query(AppSettings).filter(AppSettings.key == "http_proxy").first()
        return setting.value if setting and setting.value else ""
    finally:
        db.close()


def _get_app_setting(key: str) -> str:
    """从 app_settings 获取配置（不存在返回空字符串）"""
    db = SessionLocal()
    try:
        setting = db.query(AppSettings).filter(AppSettings.key == key).first()
        return setting.value if setting and setting.value else ""
    finally:
        db.close()


def resolve_ai_model(
    agent_name: str, stock_agent_id: int | None = None
) -> tuple[AIModel | None, AIService | None]:
    """解析 AI 模型: stock_agent 覆盖 → agent 默认 → 系统默认(is_default=True)
    返回 (model, service) 元组"""
    db = SessionLocal()
    try:
        model_id = None

        # 1. stock_agent 级别覆盖
        if stock_agent_id:
            sa = db.query(StockAgent).filter(StockAgent.id == stock_agent_id).first()
            if sa and sa.ai_model_id:
                model_id = sa.ai_model_id

        # 2. agent 级别默认
        if not model_id:
            agent = db.query(AgentConfig).filter(AgentConfig.name == agent_name).first()
            if agent and agent.ai_model_id:
                model_id = agent.ai_model_id

        # 3. 系统默认
        if not model_id:
            default_model = db.query(AIModel).filter(AIModel.is_default == True).first()
            if default_model:
                model_id = default_model.id

        # 4. 回退：取第一个
        if not model_id:
            first_model = db.query(AIModel).first()
            if first_model:
                model_id = first_model.id

        if not model_id:
            return None, None

        model = db.query(AIModel).filter(AIModel.id == model_id).first()
        if not model:
            return None, None

        service = db.query(AIService).filter(AIService.id == model.service_id).first()
        if model:
            db.expunge(model)
        if service:
            db.expunge(service)
        return model, service
    finally:
        db.close()


def resolve_notify_channels(
    agent_name: str, stock_agent_id: int | None = None
) -> list[NotifyChannel]:
    """解析通知渠道: stock_agent 覆盖 → agent 默认 → 系统默认(is_default=True)"""
    db = SessionLocal()
    try:
        channel_ids = None

        # 1. stock_agent 级别覆盖
        if stock_agent_id:
            sa = db.query(StockAgent).filter(StockAgent.id == stock_agent_id).first()
            if sa and sa.notify_channel_ids:
                channel_ids = sa.notify_channel_ids

        # 2. agent 级别默认
        if channel_ids is None:
            agent = db.query(AgentConfig).filter(AgentConfig.name == agent_name).first()
            if agent and agent.notify_channel_ids:
                channel_ids = agent.notify_channel_ids

        # 3. 按 id 列表查询或取系统默认
        if channel_ids:
            channels = (
                db.query(NotifyChannel)
                .filter(
                    NotifyChannel.id.in_(channel_ids),
                    NotifyChannel.enabled == True,
                )
                .all()
            )
        else:
            channels = (
                db.query(NotifyChannel)
                .filter(
                    NotifyChannel.is_default == True,
                    NotifyChannel.enabled == True,
                )
                .all()
            )

        for ch in channels:
            db.expunge(ch)
        return channels
    finally:
        db.close()


def _build_notifier(channels: list[NotifyChannel]) -> NotifierManager:
    """根据解析后的渠道列表构建 NotifierManager"""
    settings = Settings()
    # allow UI override via app_settings
    quiet_hours = _get_app_setting("notify_quiet_hours") or settings.notify_quiet_hours
    retry_attempts_raw = _get_app_setting("notify_retry_attempts")
    backoff_raw = _get_app_setting("notify_retry_backoff_seconds")
    overrides_raw = (
        _get_app_setting("notify_dedupe_ttl_overrides")
        or settings.notify_dedupe_ttl_overrides
    )

    try:
        retry_attempts = (
            int(retry_attempts_raw)
            if retry_attempts_raw
            else settings.notify_retry_attempts
        )
    except Exception:
        retry_attempts = settings.notify_retry_attempts
    try:
        retry_backoff_seconds = (
            float(backoff_raw) if backoff_raw else settings.notify_retry_backoff_seconds
        )
    except Exception:
        retry_backoff_seconds = settings.notify_retry_backoff_seconds

    from src.core.notify_policy import NotifyPolicy, parse_dedupe_overrides

    policy = NotifyPolicy(
        timezone=settings.app_timezone,
        quiet_hours=quiet_hours,
        retry_attempts=retry_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        dedupe_ttl_overrides=parse_dedupe_overrides(overrides_raw),
    )

    notifier = NotifierManager(policy=policy)
    for ch in channels:
        notifier.add_channel(ch.type, ch.config or {})
    return notifier


def _build_ai_client(
    model: AIModel | None, service: AIService | None, proxy: str
) -> AIClient:
    """根据解析后的 model+service 构建 AIClient"""
    if model and service:
        return AIClient(
            base_url=service.base_url,
            api_key=service.api_key,
            model=model.model,
            proxy=proxy,
        )
    # 回退到环境变量配置
    settings = Settings()
    return AIClient(
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key,
        model=settings.ai_model,
        proxy=proxy,
    )


def build_context(agent_name: str, stock_agent_id: int | None = None) -> AgentContext:
    """为指定 Agent 构建运行上下文"""
    settings = Settings()
    watchlist = load_watchlist_for_agent(agent_name)
    portfolio = load_portfolio_for_agent(agent_name)
    proxy = _get_proxy() or settings.http_proxy

    model, service = resolve_ai_model(agent_name, stock_agent_id)
    ai_client = _build_ai_client(model, service, proxy)
    channels = resolve_notify_channels(agent_name, stock_agent_id)
    notifier = _build_notifier(channels)

    model_label = f"{service.name}/{model.model}" if model and service else ""
    config = AppConfig(settings=settings, watchlist=watchlist)
    return AgentContext(
        ai_client=ai_client,
        notifier=notifier,
        config=config,
        portfolio=portfolio,
        model_label=model_label,
        notify_policy=getattr(notifier, "policy", None),
    )



def build_scheduler() -> AgentScheduler:
    """构建调度器并注册已启用的 Agent"""
    settings = Settings()
    sched = AgentScheduler(timezone=settings.app_timezone)

    # 设置 context 构建函数（每次执行时动态获取最新配置）
    sched.set_context_builder(build_context)

    db = SessionLocal()
    try:
        agent_configs = (
            db.query(AgentConfig)
            .filter(
                AgentConfig.enabled == True,
                AgentConfig.kind == AGENT_KIND_WORKFLOW,
            )
            .all()
        )
        for cfg in agent_configs:
            agent_cls = AGENT_REGISTRY.get(cfg.name)
            if not agent_cls:
                logger.warning(f"Agent {cfg.name} 未在 AGENT_REGISTRY 中注册")
                continue
            if not cfg.schedule:
                logger.info(f"Agent {cfg.name} 未设置调度计划，跳过")
                continue

            agent_kwargs = cfg.config or {}
            try:
                agent_instance = (
                    agent_cls(**agent_kwargs) if agent_kwargs else agent_cls()
                )
            except TypeError:
                agent_instance = agent_cls()
            sched.register(
                agent_instance,
                schedule=cfg.schedule,
                execution_mode=cfg.execution_mode or "batch",
            )
    finally:
        db.close()

    # 板块数据每日同步(阶段2.1/2.2): 复用主调度器的 AsyncIOScheduler,
    # 工作日 08:30 拉取行业+概念列表并写板块日线。不新建调度器。
    try:
        from src.core.thsdk_board import register_board_sync_job

        register_board_sync_job(sched.scheduler)
    except Exception as e:  # noqa: BLE001 - 注册失败不阻断调度器构建
        logger.warning(f"板块数据同步任务注册失败: {e}")

    # 数据质量哨兵(2026-08-21): 每小时跑 4 项检查, 异常写 Notification
    try:
        from src.core.data_quality_sentinel import register_hourly_job

        register_hourly_job(sched.scheduler)
    except Exception as e:  # noqa: BLE001 - 注册失败不阻断调度器构建
        logger.warning(f"数据质量哨兵注册失败: {e}")

    return sched


def reload_scheduler() -> bool:
    """重载调度器（用于配置导入/批量修改后立即生效）"""
    global scheduler
    try:
        current = globals().get("scheduler")
        if current:
            try:
                current.shutdown()
            except Exception:
                pass
        scheduler = build_scheduler()
        scheduler.start()
        logger.info("Agent 调度器已重载")
        return True
    except Exception as e:
        logger.error(f"Agent 调度器重载失败: {e}")
        return False


def _log_trigger_info(
    agent_name: str,
    stocks: list,
    model: AIModel | None,
    service: AIService | None,
    channels: list[NotifyChannel],
):
    """打印 Agent 触发时的上下文信息"""
    stock_names = ", ".join(
        f"{s.name}({s.symbol})" if hasattr(s, "symbol") else str(s) for s in stocks
    )
    ai_info = f"{service.name}/{model.model}" if model and service else "未配置"
    channel_info = ", ".join(ch.name for ch in channels) if channels else "无"
    logger.info(
        f"[触发] Agent={agent_name} | 股票=[{stock_names}] | AI={ai_info} | 通知=[{channel_info}]"
    )


def get_agent_execution_mode(agent_name: str) -> str:
    """获取 Agent 的执行模式"""
    db = SessionLocal()
    try:
        agent = db.query(AgentConfig).filter(AgentConfig.name == agent_name).first()
        return agent.execution_mode if agent and agent.execution_mode else "batch"
    finally:
        db.close()


def get_agent_config(agent_name: str) -> dict:
    """获取 Agent 的配置参数"""
    db = SessionLocal()
    try:
        agent = db.query(AgentConfig).filter(AgentConfig.name == agent_name).first()
        return agent.config if agent and agent.config else {}
    finally:
        db.close()


async def trigger_agent(agent_name: str) -> str:
    """手动触发 Agent 执行（根据执行模式处理）"""
    start = time.monotonic()
    trace_id = f"man-{agent_name}-{int(time.time() * 1000)}"
    agent_cls = AGENT_REGISTRY.get(agent_name)
    if not agent_cls:
        raise ValueError(f"Agent {agent_name} 未注册实际实现")

    with log_context(
        trace_id=trace_id,
        run_id=trace_id,
        agent_name=agent_name,
        event="trigger_agent",
        tags={"trigger_source": "manual"},
    ):
        watchlist = load_watchlist_for_agent(agent_name)
        logger.info(
            f"[watchlist] Agent={agent_name} count={len(watchlist)} symbols={[s.symbol for s in watchlist]}"
        )
        if not watchlist:
            return f"Agent {agent_name} 没有关联的自选股"

        model, service = resolve_ai_model(agent_name)
        channels = resolve_notify_channels(agent_name)
        _log_trigger_info(agent_name, watchlist, model, service, channels)

        context = build_context(agent_name)
        execution_mode = get_agent_execution_mode(agent_name)
        agent_config = get_agent_config(agent_name)

        # 根据配置初始化 Agent
        if agent_config:
            agent = agent_cls(**agent_config)
        else:
            agent = agent_cls()

        try:
            if execution_mode == "single" and hasattr(agent, "run_single"):
                # 单只模式：逐只股票分析
                results = []
                for stock in watchlist:
                    result = await agent.run_single(context, stock.symbol)
                    if result:
                        results.append(f"{stock.name}: {result.content[:100]}...")
                msg = "\n\n".join(results) if results else "无异动"
                record_agent_run(
                    agent_name=agent_name,
                    status="success",
                    result=msg,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    trace_id=trace_id,
                    trigger_source="manual",
                    model_label=context.model_label,
                )
                return msg
            else:
                # 批量模式：所有股票一起分析
                result = await agent.run(context)
                raw = result.raw_data or {}
                record_agent_run(
                    agent_name=agent_name,
                    status="success",
                    result=result.content,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    trace_id=trace_id,
                    trigger_source="manual",
                    notify_attempted=(
                        "notified" in raw
                        or "notify_error" in raw
                        or "notify_skipped" in raw
                    ),
                    notify_sent=bool(raw.get("notified", False)),
                    model_label=context.model_label,
                )
                return result.content
        except Exception as e:
            record_agent_run(
                agent_name=agent_name,
                status="failed",
                error=str(e),
                duration_ms=int((time.monotonic() - start) * 1000),
                trace_id=trace_id,
                trigger_source="manual",
                model_label=context.model_label,
            )
            raise


async def trigger_agent_for_stock(
    agent_name: str,
    stock,
    stock_agent_id: int | None = None,
    bypass_throttle: bool = False,
    bypass_market_hours: bool = False,
    suppress_notify: bool = False,
    trace_id: str | None = None,
    force_refresh: bool = False,
    user_id: str | None = None,
) -> dict:
    """手动触发 Agent 执行（单只股票）

    user_id: 触发用户 id(手动触发传入; 系统调度不传)。传入时注入 context.user,
    Agent 场景绑定走用户级模型解析(BYOK/平台授权, 见 agents/base.apply_scene_binding)。
    """
    start = time.monotonic()
    trace_id = trace_id or f"man-{agent_name}-{stock.symbol}-{int(time.time() * 1000)}"
    agent_cls = AGENT_REGISTRY.get(agent_name)
    if not agent_cls:
        raise ValueError(f"Agent {agent_name} 未注册实际实现")

    settings = Settings()
    proxy = _get_proxy() or settings.http_proxy

    try:
        market = MarketCode(stock.market)
    except ValueError:
        market = MarketCode.CN

    stock_config = StockConfig(
        symbol=stock.symbol,
        name=stock.name,
        market=market,
    )

    # 加载该股票的持仓信息
    portfolio = load_portfolio_for_stock(stock.id)

    model, service = resolve_ai_model(agent_name, stock_agent_id)
    channels = [] if suppress_notify else resolve_notify_channels(agent_name, stock_agent_id)
    _log_trigger_info(agent_name, [stock], model, service, channels)

    ai_client = _build_ai_client(model, service, proxy)
    notifier = _build_notifier(channels)

    model_label = f"{service.name}/{model.model}" if model and service else ""
    config = AppConfig(settings=settings, watchlist=[stock_config])
    context = AgentContext(
        ai_client=ai_client,
        notifier=notifier,
        config=config,
        portfolio=portfolio,
        model_label=model_label,
        suppress_notify=suppress_notify,
    )
    # 暴露 trace_id / force_refresh 给 agent(供 TradingAgents 进度反馈 + 缓存控制使用)。
    # AgentContext 不强制声明此字段,通过 setattr 注入,其他 agent 不受影响。
    setattr(context, "_trace_id", trace_id)
    setattr(context, "_force_refresh", force_refresh)

    # 用户级模型解析(2026-08-16): 手动触发带 user_id → 注入 context.user,
    # apply_scene_binding 走 BYOK/平台授权(子用户只能用被授权模型)。
    # 后台线程执行, ORM 对象跨线程不安全 → 只传 id, 此处重新加载后立即关闭会话
    # (permissions 为普通列, 加载后随实例走, 脱离会话可读)。
    if user_id:
        try:
            from src.web.database import SessionLocal
            from src.web.models import User as _User

            _db = SessionLocal()
            try:
                context.user = _db.query(_User).filter(_User.id == user_id).first()
                # 授权预检(2026-08-16): 解析器返回 None = 用户被禁用全部模型
                # (deny_all / granted 空列表 / 授权模型已删 / demo)。
                # 此时必须拦截 —— 否则 apply_scene_binding 无绑定会保留全局
                # client, 被禁用户越权使用平台模型。
                if context.user is not None:
                    from src.core.ai_client import get_model_for_scene

                    if get_model_for_scene(_db, "chat", user=context.user) is None:
                        return {
                            "skipped": True,
                            "content": "管理员未给当前用户授权任何 AI 模型,无法执行 AI 分析。请联系管理员在「用户管理 → 模型授权」中配置。",
                            "trace_id": trace_id,
                        }
            finally:
                _db.close()
        except Exception:
            logger.exception(f"加载触发用户失败(忽略, 回落系统级模型): user_id={user_id}")

    # 创建 agent，支持手动触发参数。TradingAgents 等新 agent 从 AgentConfig 读 config。
    if agent_name == "intraday_monitor":
        agent = agent_cls(
            bypass_throttle=bypass_throttle,
            bypass_market_hours=bypass_market_hours,
        )
    elif agent_name == "tradingagents":
        # 从 AgentConfig.config 读取实例化参数
        agent_kwargs = get_agent_config(agent_name) or {}
        try:
            agent = agent_cls(**agent_kwargs)
        except TypeError:
            agent = agent_cls()
    else:
        agent = agent_cls()

    with log_context(
        trace_id=trace_id,
        run_id=trace_id,
        agent_name=agent_name,
        event="trigger_agent_for_stock",
        tags={"trigger_source": "manual", "stock_symbol": stock.symbol},
    ):
        try:
            result = await agent.run(context)
            raw = result.raw_data or {}
            record_agent_run(
                agent_name=agent_name,
                status="success",
                result=result.content,
                duration_ms=int((time.monotonic() - start) * 1000),
                trace_id=trace_id,
                trigger_source="manual",
                notify_attempted=(
                    "notified" in raw
                    or "notify_error" in raw
                    or "notify_skipped" in raw
                ),
                notify_sent=bool(raw.get("notified", False)),
                model_label=model_label,
            )
        except Exception as e:
            record_agent_run(
                agent_name=agent_name,
                status="failed",
                error=str(e),
                duration_ms=int((time.monotonic() - start) * 1000),
                trace_id=trace_id,
                trigger_source="manual",
                model_label=model_label,
            )
            raise

    # 返回详细结果
    skipped = bool(result.raw_data.get("skipped", False))
    should_alert = bool(
        result.raw_data.get("should_alert", False if skipped else True)
    )
    return {
        "code": 0 if not skipped else 1001001,
        "success": not skipped,
        "message": result.content if skipped else "ok",
        "title": result.title,
        "content": result.content,
        "should_alert": should_alert,
        "notified": result.raw_data.get("notified", False),
        "skipped": skipped,
    }

