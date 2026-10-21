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
from src.core.tq_sentiment_scheduler import TqSentimentScheduler
from src.core.tq_formula_signal_scheduler import TqFormulaSignalScheduler
from src.core.market_breadth_scheduler import MarketBreadthScheduler
from src.core.decision_precompute import DecisionPrecomputeScheduler
from src.core.layer_precompute import LayerPrecomputeScheduler
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

    # APM(P1 稳定性 2026-09-18): 挂 httpx + DB 引擎耗时追踪。低开销, 失败静默。
    try:
        from src.core.apm import install_all
        from src.db.session import read_engine, write_engine

        engines = [write_engine]
        if read_engine is not write_engine:
            engines.append(read_engine)
        install_all(engines)
    except Exception as _apm_e:  # noqa: BLE001
        logger.debug(f"APM 初始化失败(忽略): {_apm_e}")

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
        # I-8 分价矩阵收盘归档(2026-10-18): 工作日 15:05 把当日分价定稿写入
        # price_distribution_daily(历史回测/主力轨迹复盘的永久事实源; 盘中快照只留 5 天)
        try:
            from src.core.price_distribution_archive import register_archive_cron

            _rs3 = locals().get("report_scheduler")
            if register_archive_cron(_rs3.scheduler if _rs3 else None):
                logger.info("分价矩阵收盘归档 cron 已注册 (工作日 15:05)")
            else:
                logger.warning("分价矩阵归档 cron 注册未生效(调度器不可用), 可手动 POST /api/market/price-distribution/{symbol}/archive")
        except Exception as e:
            logger.error(f"分价矩阵归档 cron 注册失败: {e}")
        # K线每日 backfill(收盘后 18:00, 周一至五, 拉最近 2 天)
        try:
            settings = Settings()
            rt.kline_backfill_scheduler = KlineBackfillScheduler(
                timezone=settings.app_timezone
            )
            rt.kline_backfill_scheduler.start()
        except Exception as e:
            logger.error(f"K线入库调度器启动失败: {e}")
        # TQ 市场级情绪日序列(2026-09-23)
        # 为什么需要独立调度器: 情绪周期判断要的是「今日 vs 近 20 个交易日均值」,
        # 而东财类免费源只给当日快照(没有历史序列)。TQ 的 SCJYVALUE 能给 420+ 个
        # 交易日的历史, 落到 market_sentiment_daily 才能跑 AVG/PERCENTILE。
        # 首次启动样本不足时调度器内部会自动回补 420 天。
        try:
            settings = Settings()
            rt.tq_sentiment_scheduler = TqSentimentScheduler(
                timezone=settings.app_timezone
            )
            rt.tq_sentiment_scheduler.start()
            logger.info("TQ 情绪序列调度器已启动")
        except Exception as e:
            logger.error(f"TQ情绪序列调度器启动失败: {e}")
        # TQ 条件选股信号日序列(2026-09-25)
        # 为什么需要: 108 个条件选股公式全市场扫一遍约 8s/公式且无限频, 落"当日触发
        # 家数"给市场宽度一个新维度; 免费源只有当日快照, 没历史就算不出基线。
        # 15:50 而非 15:35 —— TQ 背后只有一个客户端进程, 与情绪序列同点跑会把它压出假死。
        try:
            settings = Settings()
            rt.tq_formula_signal_scheduler = TqFormulaSignalScheduler(
                timezone=settings.app_timezone
            )
            rt.tq_formula_signal_scheduler.start()
            logger.info("TQ 条件选股信号调度器已启动")
        except Exception as e:
            logger.error(f"TQ条件选股信号调度器启动失败: {e}")

        # 市场广度日序列(2026-09-25)
        # 为什么需要: 从 PG klines 自算每日涨跌家数与六指标(ADL/ADR/ARMS/BTI/MCL/STIX)
        # +情绪温度; 不跑调度则序列不增长, 分位会一直停在最后一次同步的日期。
        # 18:40 与 TQ 类调度器(15:35/15:50)错开 —— TQ 背后只有一个客户端进程。
        try:
            settings = Settings()
            rt.market_breadth_scheduler = MarketBreadthScheduler(
                timezone=settings.app_timezone
            )
            rt.market_breadth_scheduler.start()
            logger.info("市场广度调度器已启动")
        except Exception as e:
            logger.error(f"市场广度调度器启动失败: {e}")

        # 决策合成预落库(2026-10-10 冗余设计)
        # 为什么需要: /api/decision/{symbol} 是实时计算端点(拉 120 天 K 线 + 明暗盘资金),
        # 工作台研究标签首屏就发它。盘后对全库自选∪持仓标的批算一次落 decision_cache,
        # 让盘后/次日盘前的浏览直接命中(免重算); 未预热标的仍走端上 5min TTL。
        # 15:45 与 K 线入库/TQ 类错开。
        try:
            settings = Settings()
            rt.decision_precompute_scheduler = DecisionPrecomputeScheduler(
                timezone=settings.app_timezone
            )
            rt.decision_precompute_scheduler.start()
            logger.info("决策预落库调度器已启动")
        except Exception as e:
            logger.error(f"决策预落库调度器启动失败: {e}")

        # 工作台日级图层派生块预落库(2026-10-14 首屏 P0)
        # 为什么需要: /api/klines/{symbol}/summary 的日级派生块(gs_signals/fund_flow/
        # activity/events/unlock/chips/resonance)此前每请求全量现算 —— chips/wencai/
        # pool_flow 是网络调用, 冷启动 ~26s 撞前端 20s abort, 只有 300s TTL 兜底。
        # 15:40(收盘后, K 线入库之后)批算高频票落 workbench_layer_daily; 与
        # decision_precompute(15:45)/TQ 类(15:35, 15:50)错开, 不抢客户端进程。
        try:
            settings = Settings()
            rt.layer_precompute_scheduler = LayerPrecomputeScheduler(
                timezone=settings.app_timezone
            )
            rt.layer_precompute_scheduler.start()
            logger.info("工作台图层预落库调度器已启动")
        except Exception as e:
            logger.error(f"工作台图层预落库调度器启动失败: {e}")

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
            # P1(audit-20260915): 注入 L2 事件流评估回调, 解除 seal_sampler ↔ l2_event_stream循环 import
            from src.core.l2_event_stream import install as install_l2_events

            install_l2_events()

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

        # 会话定时任务(P0, 2026-10-21): 每分钟扫一次 copilot_schedules, 到点即执行。
        # 不动态注册 job —— 重启不丢任务、删任务也没有竞态。
        try:
            from src.core.copilot_schedule import run_due as _copilot_sched_tick

            rt.scheduler.scheduler.add_job(
                _copilot_sched_tick,
                "interval",
                seconds=60,
                id="copilot-schedule-tick",
                name="参谋定时任务扫描",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("参谋定时任务扫描已注册(60s)")
        except Exception as e:
            logger.error(f"参谋定时任务扫描注册失败: {e}")

        # 撤单率时序采样(2026-10-21): 撤单率本体是"当日会话累计", 只有当前值没有曲线。
        # 交易日每 30s 采一批自选, 按分钟幂等落库, 给"买撤 60%→85%"这种拐点提供数据。
        try:
            from src.collectors.cancel_rate_sampler import run_scheduled as _cancel_sample

            rt.scheduler.scheduler.add_job(
                _cancel_sample,
                "interval",
                seconds=30,
                id="cancel-rate-sampler",
                name="撤单率时序采样",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("撤单率时序采样已注册(30s, 仅交易时段)")

        # 个股因子日快照(2026-10-21): 收盘后算一次, 给全景诊断的「位置与因子」提供横截面分位。
        try:
            from src.core.trading_calendar import is_trading_day
            from src.collectors.factor_daily import collect_once as _factor_collect

            def _factor_job():
                from datetime import datetime as _dt
                if not is_trading_day(_dt.now().date()):
                    return
                from src.db.session import SessionLocal
                db = SessionLocal()
                try:
                    _factor_collect(db)
                finally:
                    db.close()

            rt.scheduler.scheduler.add_job(
                _factor_job, "cron", hour=15, minute=35,
                id="stock-factor-daily", name="个股因子日快照",
                replace_existing=True, max_instances=1, coalesce=True,
            )
            logger.info("个股因子日快照已注册(交易日 15:35)")
        except Exception as e:
            logger.error(f"因子快照注册失败: {e}")
        except Exception as e:
            logger.error(f"撤单率采样注册失败: {e}")

        # 龙虎榜金额「次日 TQ 回填」(2026-10-20 用户要求): 交易日 08:30 回填上一个交易日。
        # 当日榜单只有东财能枚举, 而 TQ 的 GP02 金额与东财分文不差且晚一天 —— 次日用 TQ 覆写,
        # 历史数据就不再依赖"东财网关那天有没有挂"。
        try:
            from src.collectors.lhb_tq_backfill import run_scheduled as _lhb_backfill_job

            rt.scheduler.scheduler.add_job(
                _lhb_backfill_job,
                "cron",
                day_of_week="mon-fri",
                hour=8,
                minute=30,
                id="lhb-tq-backfill",
                name="龙虎榜TQ金额回填",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("龙虎榜 TQ 金额回填任务已注册(交易日 08:30)")
        except Exception as e:
            logger.error(f"龙虎榜 TQ 金额回填任务注册失败: {e}")

        # 全市场成交额/主力净流入 —— 通达信 TQ 原生公式汇总(2026-10-20 用户铁律:
        # 逆向出的客户端接口一律优先)。5576 只批量约 37s ⇒ 绝不进请求路径,
        # 5 分钟一次落 market_flow_snapshots, 接口读库。
        try:
            from src.collectors.tq_market_flow import run_scheduled as _tq_flow_job

            rt.scheduler.scheduler.add_job(
                _tq_flow_job,
                "interval",
                seconds=300,
                id="tq-market-flow",
                name="TQ全市场成交额/主力净流入",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("TQ 全市场资金汇总任务已注册(300s)")
        except Exception as e:
            logger.error(f"TQ 全市场资金汇总任务注册失败: {e}")

        # 连板梯队数据自检 + 自愈(2026-10-11 P1 断档修复): 每日 16:05(含周末)
        # limit_up_events 是 /ladder 收盘态真值源; TQ 断链期 15:35 管线静默失败 →
        # 表停更几天无人发现。本作业比对最新交易日 vs 期望交易日, 缺则 TQ 直连补数;
        # 仍缺 → 作业框架按 ok=False 判 failed(不假装成功)。放 16:05 保证在 15:35 管线之后。
        try:
            from src.core.ladder_freshness import run_ladder_freshness_job

            rt.scheduler.scheduler.add_job(
                run_ladder_freshness_job,
                "cron",
                hour=16,
                minute=5,
                id="ladder-freshness-guard",
                name="连板梯队数据自检",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("连板梯队数据自检已注册(每日 16:05)")
        except Exception as e:
            logger.error(f"连板梯队数据自检注册失败: {e}")

        # 市场情绪序列**本地自算**(2026-10-14 用户决策: 不购通达信云数据 Key)。
        # 原产者 TqSentimentScheduler(云 get_scjy_value, 15:35)永久弃用(生产断在
        # 2026-09-23, 已改为 expected_gap 静默跳过); 情绪四指标(涨停/炸板/封板资金/
        # 连板)改由客户端本地接口自算: get_zdt_data 全市场分片(≤50/次, 事故铁律) +
        # get_pricevol 取候选价量 + limit_up_events 聚合连板。落 market_sentiment_daily
        # 兼容列 + sentiment_selfcalc_daily 明细表 ⇒ 温度计/API/新鲜度自检零改动。
        # 15:45 选点: 15:35 的妖股因子增量管线(内含 limit_up_events 当日回填)已跑完,
        # 且与 15:40 共振扫描 / 15:50 条件选股(重装备 TQ)错开 —— TQ 背后只有一个客户端进程。
        try:
            from src.core.sentiment_selfcalc import run_selfcalc_job

            rt.scheduler.scheduler.add_job(
                run_selfcalc_job,
                "cron",
                day_of_week="mon-fri",
                hour=15,
                minute=45,
                id="sentiment-selfcalc-daily",
                name="市场情绪序列本地自算",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("市场情绪序列本地自算已注册(交易日 15:45)")
        except Exception as e:
            logger.error(f"市场情绪序列本地自算注册失败: {e}")

        # 市场情绪序列自检 + 自愈(2026-10-11 断链修复): 每日 16:20。
        # market_sentiment_daily(TQ get_scjy_value SC01-35 全量落库)曾停在 2026-09-22
        # 无人知 —— 每日同步(15:35)一旦失败只写一条日志, 且无表新鲜度自检(哨兵的
        # tq_gateway 只探网关可达, 不校验本表 MAX(date))。本作业比对最新交易日 vs
        # 期望交易日, 缺则 TQ 直连批量补数(带重试); 仍缺 → 作业按 ok=False 判 failed +
        # 写通知(断链显式告警)。16:20 与 TQ 类调度器(15:35/15:50)、全市场日线(16:00)、
        # 连板自检(16:05)、跨源标定(16:10)错开 —— TQ 背后只有一个客户端进程。
        try:
            from src.core.sentiment_freshness import run_sentiment_freshness_job

            rt.scheduler.scheduler.add_job(
                run_sentiment_freshness_job,
                "cron",
                hour=16,
                minute=20,
                id="sentiment-freshness-guard",
                name="市场情绪序列数据自检",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("市场情绪序列数据自检已注册(每日 16:20)")
        except Exception as e:
            logger.error(f"市场情绪序列数据自检注册失败: {e}")

        # L2 成品资金收盘快照(2026-10-11 P1): 交易日 15:05 采样落库。
        # 非交易时段 TQ get_more_info 回 0, 之前 UI 把 0 当真实值; 收盘后落快照 →
        # 盘外回退最近交易日值并显式标注(全 0 不落)。
        try:
            from src.core.l2_fund_snapshot import run_l2_snapshot_job

            rt.scheduler.scheduler.add_job(
                run_l2_snapshot_job,
                "cron",
                day_of_week="mon-fri",
                hour=15,
                minute=5,
                id="l2-fund-snapshot",
                name="L2 成品资金收盘快照",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("L2 成品资金收盘快照已注册(交易日 15:05)")
        except Exception as e:
            logger.error(f"L2 成品资金收盘快照注册失败: {e}")

        # L2 成品资金快照自检 + 自愈(2026-10-13 快照源空修复): 每日 15:35(含周末)。
        # l2_fund_snapshots 是主力净流入(L2·TQ)非交易时段回退的唯一兜底源;
        # 15:05 快照 cron 撞上 TQ 僵尸期失败后**无自愈重试** → 快照表 0 行无人知,
        # 非交易时段 web 层回退也没东西可兜(显『无数据』)。本作业比对最新交易日 vs
        # 期望交易日, 缺则复用现有采样口径重采(capture_universe); TQ 不可用显式
        # ok=False → 作业判 failed + 写通知(断链显式告警), 下次调度再试。
        # 每日跑(仿 16:05 连板/16:20 情绪自检): 周末也能发现上周断档并显式告警。
        # 15:35 与 15:05 快照 cron 错开, 且仍在 L2 会话有效窗口内(TQ get_more_info
        # 只给实时会话值, 跨日无法回补历史快照 —— 历史缺口如实告警, 不编造)。
        try:
            from src.core.l2_snapshot_freshness import run_l2_snapshot_freshness_job

            rt.scheduler.scheduler.add_job(
                run_l2_snapshot_freshness_job,
                "cron",
                hour=15,
                minute=35,
                id="l2-snapshot-freshness-guard",
                name="L2 成品资金快照数据自检",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("L2 成品资金快照数据自检已注册(每日 15:35)")
        except Exception as e:
            logger.error(f"L2 成品资金快照数据自检注册失败: {e}")

        # 首封时间×封成比 强度影子打标(2026-10-11 胜率创新 P0-1, 影子运行):
        # 交易日 15:35 对当日涨停股打强度分层标签(S/A/B/C) 落 limit_strength_shadow。
        # 独立入口(不碰 src/collectors/), 只写影子表不改任何现有信号/决策。
        try:
            from src.core.limit_strength_shadow import run_strength_shadow_job

            rt.scheduler.scheduler.add_job(
                run_strength_shadow_job,
                "cron",
                day_of_week="mon-fri",
                hour=15,
                minute=35,
                id="limit-strength-shadow",
                name="首封×封成比 强度影子打标",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("首封×封成比 强度影子打标已注册(交易日 15:35)")
        except Exception as e:
            logger.error(f"强度影子打标注册失败: {e}")

        # AI 看图形态影子打标(2026-10-14 胜率创新 · 主线一并行, 影子运行):
        # 交易日 16:30 对 **自选池 ∪ 前一日有标记票**(成本可控)逐票做
        # 「K线渲染→视觉 LLM 认形态」, 与规则形态(kline_patterns)同日同票并排落
        # ai_pattern_shadow。独立入口, 只写影子表, 不改任何现有信号/决策。
        # 视觉模型默认绑 scene='vision' → agnes-3.0-flash(sida-pro 自带多模态);
        # 无视觉模型 → 显式降级不出形态(不编造)。
        # 时段 16:30: 原 15:40 撞 TQ 采集/共振扫描高峰(15:35/15:40/15:45 已占, TQ 背后
        # 只有一个客户端进程); 16:10 已被跨源标定(indicator-calibration)占用, 故取 16:30
        # —— 在 16:00 全市场日线 / 16:05 连板自检 / 16:20 情绪自检之后, 且本作业只读 PG
        # K线 + 走外部视觉 LLM, 不碰 TQ。
        try:
            from src.core.ai_vision_pattern import run_ai_vision_pattern_job

            rt.scheduler.scheduler.add_job(
                run_ai_vision_pattern_job,
                "cron",
                day_of_week="mon-fri",
                hour=16,
                minute=30,
                id="ai-vision-pattern",
                name="AI 看图形态 影子打标(与规则形态对照)",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("AI 看图形态影子打标已注册(交易日 16:30)")
        except Exception as e:
            logger.error(f"AI 看图形态影子打标注册失败: {e}")

        # ── P0 落库加固(2026-10-18, docs/全量数据落库排查与持久化加固方案_20261018.md) ──
        # 批次 1(P0-a): 竞价过程逐分钟采样 → auction_tick_snapshots(append-only)。
        # 原 auction_snapshots 唯一键 (trade_date,symbol,market) 只留 9:25 终值,
        # 试盘/9:20 前撤单/虚假封单过程全丢。窗口/交易日由作业内自守卫, 窗口外直接 skip。
        try:
            from src.core.auction_tick_sampler import run_auction_tick_job

            rt.scheduler.scheduler.add_job(
                run_auction_tick_job,
                "cron",
                day_of_week="mon-fri",
                hour=9,
                minute="15-25",
                id="auction-tick-sampler",
                name="竞价过程帧采样(9:15-9:25 每分钟)",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("竞价过程帧采样已注册(交易日 9:15-9:25 每分钟)")
        except Exception as e:
            logger.error(f"竞价过程帧采样注册失败: {e}")

        # 批次 1(P0-a): 悟道竞价独家字段日归档(竞价一致性/买盘强度/弱转强/涨停反馈)。
        # 9:26 跑在悟道限流窗口(9:15-10:30)内 —— 大概率拿不到, 作业会显式 ok=False
        # 并留原因(不写 0 行冒充); 这是数据源事实, 不是代码缺陷。
        try:
            from src.core.auction_wudao_store import run_auction_wudao_job

            rt.scheduler.scheduler.add_job(
                run_auction_wudao_job,
                "cron",
                day_of_week="mon-fri",
                hour=9,
                minute=26,
                id="auction-wudao-daily",
                name="悟道竞价因子日归档",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("悟道竞价因子日归档已注册(交易日 9:26)")
        except Exception as e:
            logger.error(f"悟道竞价因子日归档注册失败: {e}")

        # 悟道 9:15-10:30 是服务端限流窗口(采集器 `_wudao_available` 直接跳过),
        # 9:26 那次大概率只能拿到 limited 标记。10:35 再补采一次, 才是真正有数据的一跑
        # (悟道工具在窗口后仍返回当日竞价结果)。两次同函数不同 id, 语义: 先试 → 再补。
        try:
            from src.core.auction_wudao_store import run_auction_wudao_job

            rt.scheduler.scheduler.add_job(
                run_auction_wudao_job,
                "cron",
                day_of_week="mon-fri",
                hour=10,
                minute=35,
                id="auction-wudao-daily-late",
                name="悟道竞价因子补采(限流窗口后)",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("悟道竞价因子补采已注册(交易日 10:35)")
        except Exception as e:
            logger.error(f"悟道竞价因子补采注册失败: {e}")

        # 批次 2(P0-b): TQ more_info 104 字段收盘定稿(15:10)。
        # 时序: 15:05 L2 快照 → 15:10 more_info(本作业) → 15:20 转债 → 15:30 指数。
        try:
            from src.core.more_info_daily import run_more_info_daily_job

            rt.scheduler.scheduler.add_job(
                run_more_info_daily_job,
                "cron",
                day_of_week="mon-fri",
                hour=15,
                minute=10,
                id="more-info-daily",
                name="TQ more_info 收盘定稿",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("TQ more_info 收盘定稿已注册(交易日 15:10)")
        except Exception as e:
            logger.error(f"TQ more_info 收盘定稿注册失败: {e}")

        # 批次 1(P0-a): 可转债条款刷新 + 当日折溢价/双低快照定稿(15:20)。
        try:
            from src.core.cb_radar import run_cb_daily_job

            rt.scheduler.scheduler.add_job(
                run_cb_daily_job,
                "cron",
                day_of_week="mon-fri",
                hour=15,
                minute=20,
                id="cb-daily",
                name="可转债条款+折溢价日终落库",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("可转债条款+折溢价日终落库已注册(交易日 15:20)")
        except Exception as e:
            logger.error(f"可转债日终落库注册失败: {e}")

        # 批次 2(P0-b): 指数日K落库(15:30, 首次自动回填 800 天)。
        # 落表后 /api/market/indices 的 spark 直读库, 0 次跨网(原每 30min 重付 6×K线)。
        try:
            from src.collectors.index_daily import run_index_daily_job

            rt.scheduler.scheduler.add_job(
                run_index_daily_job,
                "cron",
                day_of_week="mon-fri",
                hour=15,
                minute=30,
                id="index-daily",
                name="指数日K落库",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("指数日K落库已注册(交易日 15:30)")
        except Exception as e:
            logger.error(f"指数日K落库注册失败: {e}")

        # 龙虎榜每日增量(2026-09-10, 妖股因子 lhb 维数据底座): 交易日 17:45
        # 榜单 ~17:30 发布 → 拉近 3 个交易日落 dragon_tiger_events → 有新行的股票因子重算
        try:
            from src.core.lhb_backfill import daily_job, lhb_stats, set_recompute_hook
            from src.core.demon_factors import recompute_factors, set_lhb_stats_hook

            # P1(audit-20260915): 双向回调接线, 解除 demon_factors ↔ lhb_backfill 循环 import
            set_recompute_hook(recompute_factors)
            set_lhb_stats_hook(lhb_stats)

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

        # 龙虎榜机构/营业部明细(2026-09-10 老板"切换到东财全自动方案"): 交易日 17:50
        # 与榜单 17:45 错峰; 近 3 日幂等 upsert; 永不抛异常
        try:
            from src.core.lhb_detail_backfill import daily_job as lhb_detail_job

            rt.scheduler.scheduler.add_job(
                lhb_detail_job,
                "cron",
                day_of_week="mon-fri",
                hour=17,
                minute=50,
                id="lhb-detail-daily-backfill",
                name="龙虎榜机构/营业部明细每日回填",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("龙虎榜机构/营业部明细回填已注册(交易日 17:50)")
        except Exception as e:
            logger.error(f"龙虎榜机构/营业部明细回填注册失败: {e}")

        # 全市场日线(qfq)每日增量(v0.5.87, 老板批"全市场日线回填"): 交易日 16:00
        # 对全 A 股跑 days=10 增量 upsert, 让连板天梯等逐股日K 覆盖全市场而非仅自选池。
        try:
            from src.core.klines_daily_refresh import daily_job as klines_fullmarket_job

            rt.scheduler.scheduler.add_job(
                klines_fullmarket_job,
                "cron",
                day_of_week="mon-fri",
                hour=16,
                minute=0,
                id="klines-fullmarket-daily",
                name="全市场日线每日增量",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("全市场日线每日增量已注册(交易日 16:00)")
        except Exception as e:
            logger.error(f"全市场日线每日增量注册失败: {e}")

        # 连板梯队盘中实时(v0.5.87, 老板"盘中实时跟踪直到收盘定型"): 60s 一轮
        # scan_tick 内部自判交易时段(时段外返回 None 不写态); 15:05 后 /ladder 切 finalized。
        try:
            from src.core.limit_ladder_live import scan_tick

            rt.scheduler.scheduler.add_job(
                scan_tick, "interval", seconds=60,
                id="ladder-live-tick", name="连板梯队盘中扫描",
                replace_existing=True, max_instances=1, coalesce=True,
            )
            logger.info("连板梯队盘中扫描已注册(60s)")
        except Exception as e:
            logger.error(f"连板梯队盘中扫描注册失败: {e}")

        # 交易日历交叉校验(P1, 2026-09-13): 每日 08:00 本地 vs 通达信(近30天), 只告警不自动改本地日历
        try:
            from src.core.tdx_calendar import calendar_mismatch

            def _calendar_crosscheck() -> None:
                from datetime import datetime, timedelta

                now = datetime.now()
                start = (now - timedelta(days=30)).strftime("%Y%m%d")
                end = (now + timedelta(days=30)).strftime("%Y%m%d")
                res = calendar_mismatch(start, end)
                if not res.get("ok"):
                    logger.warning("交易日历交叉校验不可用: %s", res.get("reason"))
                    return
                lo, to = res.get("local_only") or [], res.get("tdx_only") or []
                if lo or to:
                    logger.warning("交易日历差异 local_only=%s tdx_only=%s", lo, to)
                else:
                    logger.info("交易日历交叉校验一致(%s~%s)", start, end)

            rt.scheduler.scheduler.add_job(
                _calendar_crosscheck, "cron", hour=8, minute=0,
                id="tdx-calendar-crosscheck", name="交易日历交叉校验",
                replace_existing=True, max_instances=1, coalesce=True,
            )
            logger.info("交易日历交叉校验已注册(每日08:00)")
        except Exception as e:
            logger.error(f"交易日历交叉校验注册失败: {e}")

        # 龙虎榜盘后兜底重扫(老板: "龙虎榜是每天收盘后四五点之后才有"): 19:45 / 20:00
        # 东财发布晚于 17:45 时, 当晚再扫一遍(近 3 日幂等 upsert, 有则补无则空跑)
        try:
            from src.core.lhb_backfill import daily_job as lhb_list_job
            from src.core.lhb_detail_backfill import daily_job as lhb_detail_job

            rt.scheduler.scheduler.add_job(
                lhb_list_job,
                "cron",
                day_of_week="mon-fri",
                hour=19,
                minute=45,
                id="lhb-daily-backfill-retry",
                name="龙虎榜榜单兜底重扫",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            rt.scheduler.scheduler.add_job(
                lhb_detail_job,
                "cron",
                day_of_week="mon-fri",
                hour=20,
                minute=0,
                id="lhb-detail-daily-backfill-retry",
                name="龙虎榜明细兜底重扫",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("龙虎榜盘后兜底重扫已注册(交易日 19:45/20:00)")
        except Exception as e:
            logger.error(f"龙虎榜盘后兜底重扫注册失败: {e}")

        # 大盘资金流快照采样(2026-09-11, 老板报"大盘资金流图没内容"): 每 60s
        # 快照原先只随前端调用写入 → 页面不开则日内曲线空白; 本任务独立采样(时段守卫内建)
        try:
            from src.core.market_flow_sampler import collect_once as mkt_flow_collect

            rt.scheduler.scheduler.add_job(
                mkt_flow_collect,
                "interval",
                seconds=60,
                id="market-flow-sampler",
                name="大盘资金流快照采样",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("大盘资金流快照采样已注册(每 60s, 交易时段内生效)")
        except Exception as e:
            logger.error(f"大盘资金流快照采样注册失败: {e}")

        # 板块资金采样落库(2026-10-14 资金类落库收口): board-capital-flow 此前完全
        # 现拉同花顺 + 仅 stale 备份。独立采样(180s ≈ 1~5min 区间)使页面不开也落库,
        # 读侧『够新读库、过旧回源 + stale 标注』。先例: market-flow-sampler 上游。
        try:
            from src.core.board_flow_sampler import collect_once as board_flow_collect

            rt.scheduler.scheduler.add_job(
                board_flow_collect,
                "interval",
                seconds=180,
                id="board-flow-sampler",
                name="板块资金快照采样",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("板块资金快照采样已注册(每 180s, 交易时段内生效)")
        except Exception as e:
            logger.error(f"板块资金快照采样注册失败: {e}")

        # 暗盘逐笔后台增量采集(2026-10-14, 冷启动移出请求路径): 每 60s 一轮。
        # 此前逐笔缓存过期后**在用户请求路径上**增量翻页(无缓存时整轮全量, 冷启动
        # 秒级~十几秒)。本作业对"最近被请求过"的股票(LRU 热码表, 请求路径自动登记)
        # 做增量续拉 —— 只拉缓存末游标之后的页 —— 使请求端常态命中 90s 缓存零翻页;
        # 缓存空/跨日的请求期兜底保留(冷启动一次)。间隔必须 < _TICKS_TTL(90s), 由
        # sampler_interval_s() 保证(环境变量越界会被钳回默认)。时段守卫内建(非交易
        # 时段直接 skip, 零网络请求)。
        try:
            from src.core.dark_flow_sampler import collect_once as darkflow_tick_collect
            from src.core.dark_flow_sampler import sampler_interval_s

            _interval = int(sampler_interval_s())
            rt.scheduler.scheduler.add_job(
                darkflow_tick_collect,
                "interval",
                seconds=_interval,
                id="darkflow-tick-sampler",
                name="暗盘逐笔增量采集",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("暗盘逐笔增量采集已注册(每 %ss, 交易时段内生效)", _interval)
        except Exception as e:
            logger.error(f"暗盘逐笔增量采集注册失败: {e}")

        # 三指标共振全市场扫描(2026-09-11 数智决策升级): 交易日 15:40(收盘后)
        # 用我们自己的实现(非客户端复刻公式), 落库 resonance_scan 供页面/回看
        try:
            from src.core.resonance_scan import daily_job as resonance_job

            rt.scheduler.scheduler.add_job(
                resonance_job,
                "cron",
                day_of_week="mon-fri",
                hour=15,
                minute=40,
                id="resonance-scan-daily",
                name="三指标共振全市场扫描",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("三指标共振全市场扫描已注册(交易日 15:40)")
        except Exception as e:
            logger.error(f"三指标共振扫描注册失败: {e}")

        # 三指标共振 AI 批量判定(2026-09-11 老板"可以"): 交易日 15:50(扫描后 10 分钟)
        try:
            from src.core.resonance_ai import run_daily_verdicts

            def _resonance_ai_job() -> None:
                import asyncio

                try:
                    asyncio.run(run_daily_verdicts(limit=24))
                except Exception as e:  # noqa: BLE001
                    logger.warning("共振 AI 批量判定任务异常: %s", e)

            rt.scheduler.scheduler.add_job(
                _resonance_ai_job,
                "cron",
                day_of_week="mon-fri",
                hour=15,
                minute=50,
                id="resonance-ai-daily",
                name="三指标共振 AI 批量判定",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("三指标共振 AI 批量判定已注册(交易日 15:50)")
        except Exception as e:
            logger.error(f"共振 AI 批量判定注册失败: {e}")

        # 口径快照留痕(B5, 2026-09-18): 交易日 15:55 —— 三源逐日留痕, 供"口径漂移曲线"与数字可溯源。
        # 放在 15:45 题材情绪分之后, 让当日的涨停/成交都已落定; 只采小样本(上限 40 只/次)。
        try:
            from src.web.api.caliber_archive import record_symbol as _caliber_record
            from src.web.api.caliber_archive import get_archive_symbols as _caliber_symbols

            def _caliber_archive_job() -> None:
                from datetime import date

                from src.core.trading_calendar import is_trading_day
                from src.db.session import get_write_engine

                if not is_trading_day(date.today()):
                    logger.info("口径快照留痕: 非交易日, 跳过")
                    return
                eng = get_write_engine()
                ok = 0
                for code in _caliber_symbols(eng):
                    try:
                        res = _caliber_record(eng, code)
                        ok += 1
                        logger.info("口径快照留痕 %s: %s 行 %s", code, res.get("rows"), res.get("available_by_source"))
                    except Exception as e:  # noqa: BLE001 —— 单票失败不拖垮整批
                        logger.warning("口径快照留痕 %s 失败: %r", code, e)
                logger.info("口径快照留痕完成: %s 只", ok)

            rt.scheduler.scheduler.add_job(
                _caliber_archive_job,
                "cron",
                day_of_week="mon-fri",
                hour=15,
                minute=55,
                id="caliber-archive-daily",
                name="口径快照留痕",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("口径快照留痕已注册(交易日 15:55)")
        except Exception as e:
            logger.error(f"口径快照留痕注册失败: {e}")

        # 每日跨源指标标定(2026-10-10, Volume 单位事故教训): 交易日 16:10。
        # 抽样 N 只对 腾讯/东财/TQ 三源快照做 单源恒等式 + 跨源一致性 两级校验;
        # 异常显式落 datasource_failures/告警, 单源缺失显式降级; 作业 ok=False → failed。
        # 16:10 与 TQ 类调度器(15:35/15:40/15:50)+全市场日线(16:00)错开(TQ 单客户端进程)。
        try:
            from src.core.indicator_calibration import daily_job as _calibration_job

            rt.scheduler.scheduler.add_job(
                _calibration_job,
                "cron",
                day_of_week="mon-fri",
                hour=16,
                minute=10,
                id="indicator-calibration-daily",
                name="每日跨源指标标定",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("每日跨源指标标定已注册(交易日 16:10)")
        except Exception as e:
            logger.error(f"每日跨源指标标定注册失败: {e}")

        # 题材情绪分(2026-10-08): 盘中实时刷新(交易日 9:30-15:00 每 10 分钟,
        # source='intraday') + 收盘定型(15:05, source='close'+settled_at, 幂等)。
        # 取代旧的单一 15:45 扫描 —— 15:05 定型早于 demon 落涨停事件无关紧要:
        # 题材情绪的涨停事实由日线 + limit_rules **自算**(不依赖 limit_up_events)。
        # 非交易日两 job 都不跑(为工作日专 cron + 任务内交易日守卫双保险)。
        try:
            from src.web.api.theme_mood import (
                intraday_refresh_job,
                settle_refresh_job,
            )

            # 用字符串 "cron" + kwargs(而非 CronTrigger 实例): 只有字符串形式才会注入
            # 调度器的 app_timezone, 触发器实例会退回进程本地时区(与全文件的写法一致)。
            rt.scheduler.scheduler.add_job(
                settle_refresh_job,
                "cron",
                day_of_week="mon-fri",
                hour=15,
                minute=5,
                id="theme-mood-settle",
                name="题材情绪收盘定型",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            rt.scheduler.scheduler.add_job(
                intraday_refresh_job,
                "cron",
                day_of_week="mon-fri",
                hour="9-15",
                minute="*/10",
                id="theme-mood-intraday",
                name="题材情绪盘中刷新",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("题材情绪已注册: 盘中刷新(9:30-15:00 每10分钟) + 收盘定型(15:05)")
        except Exception as e:
            logger.error(f"题材情绪调度注册失败: {e}")

        # 连板梯队定型预渲染(2026-10-14): 交易日 16:30 把当日定型 payload 渲进
        # ladder_daily_snapshot —— /ladder 每请求现算 6.5s(20 天涨停梯队 + 1100+ 只
        # 个股 OHLC), 请求期改读库(毫秒级) + 盘中只叠加 live 快照当日行。
        # 16:30 在 limit_up_events 15:35 管线 + 16:05 自检 + 16:00 全市场日线之后
        # (梯队日K蜡烛要当日 klines), 且本作业纯读库不抢 TQ 客户端; 幂等(同日覆盖)。
        try:
            from src.web.api.theme_mood import ladder_snapshot_job

            rt.scheduler.scheduler.add_job(
                ladder_snapshot_job,
                "cron",
                day_of_week="mon-fri",
                hour=16,
                minute=30,
                id="theme-mood-ladder-snapshot",
                name="连板梯队定型预渲染",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("连板梯队定型预渲染已注册(交易日 16:30)")
        except Exception as e:
            logger.error(f"连板梯队定型预渲染注册失败: {e}")

        # 盘中 DDE 大单逐 N 分钟采样(P3 补差 A, 2026-10-10): 落 dde_minute_flow 序列表,
        # 解锁分时突破「突」的『DDE大单持续流入』判定(此前只有当日快照 → 恒显式降级)。
        # 节拍走 thresholds(minute_dde_sample_min, 默认 5min); 分钟位从 :01 起跳**避开整点拥堵**;
        # 非交易时段由任务内守卫显式跳过(不落行、不建作业行)。
        try:
            from src.core import thresholds as _th
            from src.core.dde_sampler import run_dde_sample_job

            _n = max(1, min(60, int(_th.value("minute_dde_sample_min"))))
            rt.scheduler.scheduler.add_job(
                run_dde_sample_job,
                "cron",
                day_of_week="mon-fri",
                hour="9-15",
                minute=f"1-59/{_n}",
                id="dde-intraday-sample",
                name="DDE大单逐分钟采样",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("DDE大单采样已注册(交易日 9:30-15:00 每 %d 分钟, 避整点)", _n)
        except Exception as e:
            logger.error(f"DDE大单采样注册失败: {e}")

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

        # 十档盘口快照采集(2026-10-14, 盘口移出用户请求路径): 交易时段每 15~30s
        # (ORDERBOOK_SNAPSHOT_INTERVAL_SEC, 默认 20s)对活跃票(自选∪持仓∪当日异动, 带上限)
        # 拉 thsdk 十档写 orderbook_snapshots。请求端只读快照 + snapshot_age_sec 标注,
        # 超龄才穿透实时 —— 把 8~12s 的 thsdk 硬超时从用户请求链里挪到后台。
        # 节流(universe 上限 + 单轮时间预算 + 逐票小睡)防 thsdk 过载; 时段守卫在模块内。
        try:
            from src.core import orderbook_snapshot as _obsnap

            _ob_interval = _obsnap.snapshot_interval_sec()
            rt.scheduler.scheduler.add_job(
                _obsnap.collect_once,
                "interval",
                seconds=_ob_interval,
                id="orderbook-snapshot-sampler",
                name="十档盘口快照采集",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info(
                "十档盘口快照采集已注册(interval %ds, 交易时段内生效, 上限 %d 只)",
                _ob_interval, _obsnap.max_symbols(),
            )
        except Exception as e:
            logger.error(f"十档盘口快照采集注册失败: {e}")

        # 分钟K线 1m 滚动入库(2026-09-10, 数据落库设计 Matrix #2): 每 60s, 交易时段由模块内守卫
        try:
            from src.core.klines_minute import collect_minute_klines_once

            rt.scheduler.scheduler.add_job(
                collect_minute_klines_once,
                "interval",
                seconds=60,
                id="klines-minute-1min",
                name="分钟K线1m滚动入库",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("分钟K线1m滚动入库已注册(interval 60s, 时段守卫在模块内)")
        except Exception as e:
            logger.error(f"分钟K线1m滚动入库注册失败: {e}")

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

        # 决策账本回填(P0-1 审计修复, 2026-10-10): 交易日 18:35 回填 T+1/3/5 收益与命中。
        # 此前 decision_log.backfill_outcomes 全仓无生产调度(仅测试调用, 生产引用只剩一条注释)
        # → DecisionLedger 的 ret_t1/hit_t1 从不被回填, 命中率恒显『样本不足』, 反馈环整段死。
        # 仿 signal-nightly-review(18:30) 错开 5 分钟; 非交易日跳过(双保险); 失败显式。
        try:
            from src.core.decision_log import register_cron as _register_decision_backfill

            if _register_decision_backfill(rt.scheduler.scheduler):
                logger.info("决策账本回填 cron 已注册 (交易日 18:35)")
            else:
                logger.warning("决策账本回填 cron 注册未生效(调度器不可用)")
        except Exception as e:
            logger.error(f"决策账本回填 cron 注册失败: {e}")

        # GDPR 用户数据物理清除(P3, 2026-09-18): 每日 03:20 清除软删除满 30 天的账号
        try:
            from src.web.api.user_data import purge_expired_deletions

            rt.scheduler.scheduler.add_job(
                purge_expired_deletions,
                "cron",
                hour=3,
                minute=20,
                id="user-data-purge",
                name="注销用户数据物理清除",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("注销用户数据物理清除已注册(每日 03:20)")
        except Exception as e:
            logger.error(f"注销用户数据物理清除注册失败: {e}")

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
    if rt.tq_sentiment_scheduler:
        rt.tq_sentiment_scheduler.shutdown()
    if rt.tq_formula_signal_scheduler:
        rt.tq_formula_signal_scheduler.shutdown()
    if rt.decision_precompute_scheduler:
        rt.decision_precompute_scheduler.shutdown()
    if rt.layer_precompute_scheduler:
        rt.layer_precompute_scheduler.shutdown()
    # v0.4.36 P0 派活 1: WS Hub 解绑 loop
    try:
        from src.web.notifications.ws_hub import attach_event_loop
        attach_event_loop(None)
    except Exception:
        pass
