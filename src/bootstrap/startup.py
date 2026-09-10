"""应用 lifespan: 启动门禁 → init_db → 自检 → 种子 → 调度器编排(原 server.py 搬移)。

W3.2/D1: 调度器全局实例统一写在 src.bootstrap.runtime 命名空间(rt.*),
server.py shim 经 PEP 562 转发, health 深检等读到的仍是 live 值。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from src.bootstrap import runtime as rt
from src.bootstrap.agents import seed_agents
from src.bootstrap.datasources import (
    reconcile_data_sources,
    seed_sample_stocks,
    seed_strategies,
)
from src.bootstrap.env import setup_logging, setup_playwright, setup_proxy, setup_ssl
from src.config import Settings
from src.core.context_scheduler import ContextMaintenanceScheduler
from src.core.kline_backfill_scheduler import KlineBackfillScheduler
from src.core.paper_trading_scheduler import PaperTradingScheduler
from src.core.price_alert_scheduler import PriceAlertScheduler
from src.core.report_scheduler import ReportScheduler
from src.web.database import SessionLocal, init_db

logger = logging.getLogger("server")


@asynccontextmanager
async def lifespan(app):
    """应用生命周期: 初始化 + 启动调度器"""
    # 0.4② (2026-09-08) 方言门禁 fail-stop: 丢 SIDA_DB_URL 曾静默回退 SQLite 跑 4 天。
    # 必须在 init_db 之前把关 —— 配置不明确直接终止启动, 而不是带病起服务。
    from src.core.startup_check import check_db_dialect_explicit

    _gate_ok, _gate_msg = check_db_dialect_explicit()
    if not _gate_ok:
        logger.error("[启动门禁] %s", _gate_msg)
        raise RuntimeError(f"启动门禁未通过: {_gate_msg}")
    # 热修 2026-08-14: SIGCHLD 置 SIG_IGN, 让内核自动回收子进程(healthcheck 超时 fork 的 python
    # 子进程变僵尸堆积 87+ 个的根因; 容器 PID1 默认不 reap)。
    import signal
    try:
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    except Exception:
        pass
    init_db()
    # 2026-09-01 审计修复: 确保 report_scheduler 局部变量始终绑定(默认 None),
    # 否则当 _leader_ok 为 False 或 ReportScheduler 构造抛异常(被 except 吞)时,
    # 下方 shutdown 的 `if report_scheduler:` 会触发 UnboundLocalError, 且报告
    # 调度器可能静默未启动。初始化后即可安全引用(未启动则为 None, 不进入 shutdown)。
    report_scheduler = None
    # 启动配置自检(2026-08-21): fail-fast 告警, 不阻断启动
    try:
        from src.core.startup_check import run_startup_checks

        run_startup_checks()
    except Exception:
        logger.warning("启动自检异常(跳过, 不阻断启动)", exc_info=True)
    # 2026-08-18: 主动连 Redis (之前只在 close() 路径释放,健康检查报 down)
    try:
        from src.web.cache.redis_client import redis_client as _rc
        await _rc.connect()
    except Exception as e:
        logger.warning(f"[Redis] 启动连接失败,降级运行: {e}")
    # 2026-08-22: biz_cache 是懒连接(首次 set/get 时才连),无需显式预热;
    # 但这里主动触发一次连接,让 /health 的 biz_cache.redis 字段在启动后即为 ok/down
    try:
        from src.web.cache.biz_cache import biz_cache as _bc
        _bc._ensure_redis()
    except Exception as e:
        logger.warning(f"[biz-cache] 启动预热失败(懒连接兜底): {e}")
    setup_logging()
    setup_proxy()  # 设置进程 env 代理(HTTP_PROXY/NO_PROXY);所有 httpx(trust_env=True)据此走代理
    setup_ssl()
    setup_playwright()

    # 从环境变量初始化认证（Docker 部署用）
    from src.web.api.auth import init_auth_from_env

    db = SessionLocal()
    try:
        if init_auth_from_env(db):
            logger.info("已从环境变量初始化认证账号")
    finally:
        db.close()

    seed_agents()
    try:
        db = SessionLocal()
        try:
            reconcile_data_sources(db)
        finally:
            db.close()
    except Exception as e:
        logger.warning(f"数据源对账失败,跳过(不阻断启动): {e}")
    seed_strategies()
    seed_sample_stocks()

    # 启动时回填历史 TradingAgents 决策到建议池(stock_suggestions)
    # 早期 TA 运行没写建议池,这次启动一次性补齐,让「AI 建议」面板能看到。
    # 幂等:已存在不重复写;每次启动重跑代价极低(只查最近 7 天 + dedupe)。
    try:
        from src.agents.tradingagents.backfill import backfill_tradingagents_suggestions
        backfill_tradingagents_suggestions(days=7)
    except Exception as e:
        logger.warning(f"TradingAgents 建议回填失败,跳过: {e}")

    # 后台刷新股票列表缓存
    import threading
    from src.collectors.stock_list import get_stock_list, refresh_stock_list

    def refresh_stock_cache():
        stocks = get_stock_list()
        if not stocks or len([s for s in stocks if s["market"] == "CN"]) == 0:
            logger.info("股票列表缓存为空或缺少 A 股，后台刷新中...")
            refresh_stock_list()

    threading.Thread(target=refresh_stock_cache, daemon=True).start()

    # 调度器选主(2026-08-23 Q1): 多 uvicorn worker 下只有租约持有者启动
    # 调度器与微信 BOT, 防止定时任务双跑; SIDA_ENABLE_SCHEDULERS=1/0 可强制
    from src.core.scheduler_leader import try_acquire as _try_sched_leader
    _leader_ok, _leader_why = _try_sched_leader()
    if not _leader_ok:
        logger.info("[选主] 本 worker 不启动调度器/微信BOT: %s", _leader_why)
    if _leader_ok:
        rt._kline_oneoff_loop = asyncio.get_running_loop()  # 跨线程调度用
        # v0.4.36 P0 派活 1: WS Hub 绑定到 running loop (broadcast 跨线程投递用)
        try:
            from src.web.notifications.ws_hub import attach_event_loop, install_pubsub_listener
            attach_event_loop(rt._kline_oneoff_loop)
            install_pubsub_listener()
        except Exception as e:
            logger.warning(f"[WS-Hub] 启动绑定失败(降级运行): {e}")
        rt.scheduler = rt.build_scheduler()
        rt.scheduler.start()
        logger.info("Agent 调度器已启动")
        try:
            settings = Settings()
            rt.price_alert_scheduler = PriceAlertScheduler(
                timezone=settings.app_timezone,
                interval_seconds=60,
            )
            rt.price_alert_scheduler.start()
            logger.info("价格提醒调度器已启动")
        except Exception as e:
            logger.error(f"价格提醒调度器启动失败: {e}")
        try:
            settings = Settings()
            rt.paper_trading_scheduler = PaperTradingScheduler(
                timezone=settings.app_timezone,
                interval_seconds=60,
            )
            rt.paper_trading_scheduler.start()
            logger.info("模拟盘调度器已启动")
        except Exception as e:
            logger.error(f"模拟盘调度器启动失败: {e}")
        try:
            settings = Settings()
            rt.context_maintenance_scheduler = ContextMaintenanceScheduler(
                timezone=settings.app_timezone,
                eval_interval_hours=6,
                snapshot_retention_days=180,
                outcome_retention_days=365,
            )
            rt.context_maintenance_scheduler.start()
            logger.info("上下文维护调度器已启动")
        except Exception as e:
            logger.error(f"上下文维护调度器启动失败: {e}")
        # SIDA 内置报告生成器(盘前 8:30 / 盘后 15:30, 周一至五)
        try:
            settings = Settings()
            report_scheduler = ReportScheduler(timezone=settings.app_timezone)
            report_scheduler.start()
            logger.info("SIDA 报告调度器已启动")
        except Exception as e:
            logger.error(f"SIDA 报告调度器启动失败: {e}")
        # 竞价异动同步 job(v0.3.0 阶段1.2): 复用 report_scheduler 的底层 APScheduler,
        # 不新开 scheduler。工作日 09:25 拉竞价异动股落库(OFF-hook, 失败不崩)。
        try:
            settings = Settings()
            from src.core.auction_pool import register_cron

            if not register_cron(locals().get("report_scheduler", None) and locals().get("report_scheduler").scheduler):
                logger.warning("竞价异动 cron 注册未生效(调度器不可用), 09:25 同步将跳过")
        except Exception as e:
            logger.error(f"竞价异动 cron 注册失败: {e}")
        # 情绪周期每日同步(v0.4.6): 工作日 15:10 收盘后自动 sync 涨停池指标落库
        try:
            from src.web.api.market_phase import register_cron as _phase_register_cron

            _rs = locals().get("report_scheduler")
            if _phase_register_cron(_rs.scheduler if _rs else None):
                logger.info("情绪周期每日同步 cron 已注册 (工作日 15:10)")
            else:
                logger.warning("情绪周期 cron 注册未生效(调度器不可用), 阶段数据需手动 sync")
        except Exception as e:
            logger.error(f"情绪周期 cron 注册失败: {e}")
        # K线盘前预缓存(v0.4.10): 工作日 09:20 主动增量入库自选+候选池,
        # 开盘后消费者直接命中 PG 缓存, 对外请求数砍 ~80%
        try:
            from src.core.kline_precache import register_precache_cron

            _rs2 = locals().get("report_scheduler")
            if register_precache_cron(_rs2.scheduler if _rs2 else None):
                logger.info("K线盘前预缓存 cron 已注册 (工作日 09:20)")
            else:
                logger.warning("K线盘前预缓存 cron 注册未生效")
        except Exception as e:
            logger.error(f"K线盘前预缓存 cron 注册失败: {e}")
        # K线每日 backfill(收盘后 18:00, 周一至五, 拉最近 2 天)
        try:
            settings = Settings()
            rt.kline_backfill_scheduler = KlineBackfillScheduler(
                timezone=settings.app_timezone
            )
            rt.kline_backfill_scheduler.start()
        except Exception as e:
            logger.error(f"K线入库调度器启动失败: {e}")

        # L2 逐笔定期落库(v0.4.77): 每 5 分钟一次, 盘中拉自选+候选池 thsdk L2 → DB,
        # 前端 /api/klines/{symbol}/l2-ticks 默认 fetch=0 只读库, 解决 30s 超时
        try:
            from src.core.l2_ticks_scheduler import L2TicksScheduler
            settings = Settings()
            _l2_sched = L2TicksScheduler(tz_name=settings.app_timezone)
            _l2_sched.start()
            logger.info("L2 逐笔 5min cron 已启动")
        except Exception as e:
            logger.error(f"L2 逐笔 cron 启动失败: {e}")

        # 封单成色盘中采样(批次A, 2026-09-06): 60s 一次, 交易时段判断在任务内
        try:
            from src.core.seal_sampler import sample_tick

            rt.scheduler.scheduler.add_job(
                sample_tick,
                "interval",
                seconds=60,
                id="seal-quality-sampler",
                name="封单成色采样",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("封单成色采样任务已注册(60s)")
        except Exception as e:
            logger.error(f"封单成色采样任务注册失败: {e}")

        # 妖股因子增量管线(批次B 存档化, 2026-09-06): 交易日 15:35
        # 增量回填(只拉近15根K线,只补新日期) → 新事件股票因子重算 → demon_factors 落档
        try:
            from src.core.demon_factors import update_pipeline

            rt.scheduler.scheduler.add_job(
                update_pipeline,
                "cron",
                day_of_week="mon-fri",
                hour=15,
                minute=35,
                id="demon-factor-pipeline",
                name="妖股因子增量管线",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("妖股因子增量管线已注册(交易日 15:35)")
        except Exception as e:
            logger.error(f"妖股因子增量管线注册失败: {e}")

        # 龙虎榜每日增量(2026-09-10, 妖股因子 lhb 维数据底座): 交易日 17:45
        # 榜单 ~17:30 发布 → 拉近 3 个交易日落 dragon_tiger_events → 有新行的股票因子重算
        try:
            from src.core.lhb_backfill import daily_job

            rt.scheduler.scheduler.add_job(
                daily_job,
                "cron",
                day_of_week="mon-fri",
                hour=17,
                minute=45,
                id="lhb-daily-backfill",
                name="龙虎榜每日增量回填",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("龙虎榜每日增量回填已注册(交易日 17:45)")
        except Exception as e:
            logger.error(f"龙虎榜每日增量回填注册失败: {e}")

        # 快照行情 1 分钟桶落库(批次2 2/2, 2026-09-10): 每 60s, 交易时段由模块内守卫
        try:
            from src.core.quote_snapshots import collect_once

            rt.scheduler.scheduler.add_job(
                collect_once,
                "interval",
                seconds=60,
                id="quote-snapshots-1min",
                name="快照行情1分钟落库",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("快照行情1分钟落库已注册(interval 60s, 时段守卫在模块内)")
        except Exception as e:
            logger.error(f"快照行情1分钟落库注册失败: {e}")

        # 信号对账(批次D 复盘闭环, 2026-09-06): 交易日 18:30 回填 T+1/T+5 收益
        try:
            from src.core.signal_review import nightly_review

            rt.scheduler.scheduler.add_job(
                nightly_review,
                "cron",
                day_of_week="mon-fri",
                hour=18,
                minute=30,
                id="signal-nightly-review",
                name="信号复盘对账",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("信号对账任务已注册(交易日 18:30)")
        except Exception as e:
            logger.error(f"信号对账任务注册失败: {e}")

        # 微信数智分析BOT worker: 长轮询 getupdates, 微信消息 → AI 回复 → 回微信
        try:
            from src.core.wechat_bot_worker import wechat_bot_worker

            app.state.wechat_bot_task = asyncio.create_task(wechat_bot_worker(), name="wechat-bot-worker")
            logger.info("微信数智分析BOT worker 已启动")
        except Exception as e:
            logger.error(f"微信数智分析BOT worker 启动失败: {e}")
    yield
    # 2026-08-17 v0.2.65: Redis 客户端关闭
    try:
        from src.web.cache.redis_client import redis_client as _redis_close
        await _redis_close.close()
    except Exception:
        pass
    # P2-4 (2026-09-05 28号审计): 微信 BOT worker shutdown 时 cancel, 防重启残留
    try:
        _bot_task = getattr(app.state, "wechat_bot_task", None)
        if _bot_task is not None and not _bot_task.done():
            _bot_task.cancel()
            logger.info("微信数智分析BOT worker 已取消")
    except Exception:
        pass

    if rt.scheduler:
        rt.scheduler.shutdown()
        logger.info("Agent 调度器已关闭")
    if rt.price_alert_scheduler:
        rt.price_alert_scheduler.shutdown()
        logger.info("价格提醒调度器已关闭")
    if rt.paper_trading_scheduler:
        rt.paper_trading_scheduler.shutdown()
        logger.info("模拟盘调度器已关闭")
    if rt.context_maintenance_scheduler:
        rt.context_maintenance_scheduler.shutdown()
        logger.info("上下文维护调度器已关闭")
    if report_scheduler:
        report_scheduler.shutdown()
        logger.info("SIDA 报告调度器已关闭")
    if rt.kline_backfill_scheduler:
        rt.kline_backfill_scheduler.shutdown()
    # v0.4.36 P0 派活 1: WS Hub 解绑 loop
    try:
        from src.web.notifications.ws_hub import attach_event_loop
        attach_event_loop(None)
    except Exception:
        pass
