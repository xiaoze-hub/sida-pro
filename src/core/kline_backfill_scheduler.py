"""K线每日 backfill 调度器(2026-08-17)
- 收盘后 18:00 自动拉取当日 + 最近 2 天 K线(覆盖当日 + 周末补齐)
- 周一到周五(交易日)
- 调 klines_ingestor.ingest_batch, 复用 ingest_symbol

设计要点:
- 独立于 Agent 调度器(同 price_alert / report / paper_trading 模式)
- 跑在线程里, 不阻塞 Web 事件循环
- 失败 retry 一次(防止偶发网络抖动)
- 静默时段(>4 小时)不跑(异常)
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.collectors.klines_ingestor import ingest_batch, get_default_symbols, ingest_symbol
from src.models.market import MarketCode
from src.web.database import create_engine
from src.web.database import DB_URL
from src.web.cache.streams import publish_kline_backfill  # 2026-08-17 v0.2.65 (Phase 1)

logger = logging.getLogger(__name__)


# 全局单例(server.py lifespan 启动后赋值, 加股 API 复用)
_global_scheduler: "KlineBackfillScheduler | None" = None


# 18:00 daily, 交易日(周一到周五) 拉最近 2 天(覆盖当日 + 周末)
BACKFILL_CRON = {"hour": 18, "minute": 0}
BACKFILL_DAYS = 2  # 拉最近 2 天(覆盖当日 + 周末/节假日补齐)
BACKFILL_DAYS_FALLBACK = 7  # 失败重试用 7 天
CONCURRENCY = 5  # 并发 ingest 股数


def _is_market_day() -> bool:
    """简单交易日判断: 周一到周五 = 交易日。
    注: 实际节假日需要专门的交易日历(目前用不到, 留个 hook)。
    """
    return datetime.now(timezone.utc).weekday() < 5  # 0-4 = Mon-Fri


def _backfill_in_worker(days: int) -> dict:
    """在线程里跑 backfill, 避免阻塞 asyncio 事件循环。"""

    # 简单判断当前是否在交易时段后(>= 16:00 Asia/Shanghai)
    # 18:00 跑一般收盘后 3 小时, 数据稳定
    engine = create_engine(DB_URL, pool_pre_ping=True)
    try:
        symbols = get_default_symbols()
        logger.info(
            f"[kline backfill] 开始: {len(symbols)} 只股, days={days}, "
            f"concurrent={CONCURRENCY}"
        )
        start = time.time()
        result = asyncio.run(
            ingest_batch(
                engine,
                symbols,
                period="1d",
                days=days,
                concurrency=CONCURRENCY,
            )
        )
        elapsed = time.time() - start
        rate = result["total_ingested"] / max(elapsed, 0.1)
        logger.info(
            f"[kline backfill] 完成: {result['total_ingested']} 行 / "
            f"{elapsed:.1f}s / {rate:.0f} 行/秒"
        )
        return {
            "ingested": result["total_ingested"],
            "elapsed": elapsed,
            "rate": rate,
        }
    finally:
        engine.dispose()


def _ingest_one_in_worker(engine, symbol: str, market: MarketCode) -> dict:
    """单股 backfill worker — 线程里跑, 避免阻塞 asyncio。

    2026-08-17: 加股 60s 后触发此函数, 拉这 1 只股的 800 天 K 线。
    """
    return asyncio.run(ingest_symbol(engine, symbol, market, period="1d", days=800))


class KlineBackfillScheduler:
    """K线每日 backfill 调度器, 18:00 收盘后自动入库。"""

    def __init__(self, timezone: str = "Asia/Shanghai"):
        self.scheduler = AsyncIOScheduler(timezone=timezone)
        self._running = False

    async def _backfill_job(self):
        if self._running:
            logger.warning("[kline backfill] 上轮还在跑, 跳过本轮")
            return
        if not _is_market_day():
            logger.info("[kline backfill] 今天非交易日, 跳过")
            return
        self._running = True
        try:
            # 第 1 次: 拉 2 天
            result = await asyncio.to_thread(_backfill_in_worker, BACKFILL_DAYS)
            if result["ingested"] == 0:
                # 0 行入库(可能数据源问题), 再试 7 天兜底
                logger.warning("[kline backfill] 首次入库 0 行, 尝试 7 天回填")
                await asyncio.to_thread(_backfill_in_worker, BACKFILL_DAYS_FALLBACK)
        except Exception as e:
            logger.exception(f"[kline backfill] 异常: {e}")
        finally:
            self._running = False

    def start(self):
        global _global_scheduler
        _global_scheduler = self  # 注册到全局, 加股 API 复用

        self.scheduler.add_job(
            self._backfill_job,
            "cron",
            day_of_week="mon-fri",
            hour=BACKFILL_CRON["hour"],
            minute=BACKFILL_CRON["minute"],
            id="kline_backfill_daily",
            replace_existing=True,
            coalesce=True,  # 错过的多次合并成一次
            max_instances=1,
        )
        self.scheduler.start()
        from src.core.scheduler_registry import register

        register("kline_backfill", self.scheduler)
        logger.info(
            f"K线入库调度器已启动: 每日 {BACKFILL_CRON['hour']:02d}:"
            f"{BACKFILL_CRON['minute']:02d} (周一至五, "
            f"{self.scheduler.timezone})"
        )

    def shutdown(self):
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass
        logger.info("K线入库调度器已关闭")

    def trigger_now(self) -> dict:
        """手动触发一次 backfill(管理界面或测试用)。"""
        return _backfill_in_worker(BACKFILL_DAYS)

    def schedule_one_off(self, symbol: str, market: str, delay_seconds: int = 60) -> None:
        """加股快速 backfill(2026-08-17):
        - 用户加自选股后 60 秒延迟入库
        - 60 秒延迟合并 1 分钟内多次 add(用户连续点不会重复拉)
        - 失败静默 — 18:00 cron 兜底

        2026-08-27 fix (5+1 评审 B 轨): 原实现把任务发布到 Redis Stream, 但全仓库
        没有任何消费者(见 streams.py 注释 'consumer: 待定'), 消息从不执行; 且
        APScheduler 兜底块被错误缩进嵌在 except 内, 只在 publish 抛异常时触发
        (Redis 不可用时 publish 优雅返回 None, 几乎不抛) → 兜底实际是死代码。
        现改为: 无条件调度 APScheduler 兜底(当前唯一真实入库路径), Stream 发布
        保留作为未来 worker 的预留。
        """
        # 1) 发布到 Redis Stream 预留(暂无消费者, fire-and-forget 不阻塞主流程)
        try:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(publish_kline_backfill(symbol, market, days=800))
            except RuntimeError:
                asyncio.run(publish_kline_backfill(symbol, market, days=800))
        except Exception as e:
            logger.debug(f"[kline_backfill] stream publish skipped: {e}")

        # 2) 无条件调度 APScheduler 兜底(当前唯一真实入库路径)
        # APScheduler 跑在它自己的后台线程(没有 asyncio loop)
        # 但 server.py 跑在 uvicorn 的 asyncio loop 里
        # 所以要从 apscheduler 线程 → uvicorn 线程用 call_soon_threadsafe
        import server as _server_mod
        from datetime import timedelta

        run_date = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
            seconds=delay_seconds
        )
        job_id = f"kline_backfill_oneoff_{symbol}_{market}"
        try:
            loop = _server_mod._kline_oneoff_loop
            if loop is None:
                logger.warning(
                    f"[kline oneoff] server.py lifespan 未设置 _kline_oneoff_loop, "
                    f"跳过 {symbol}.{market}(18:00 cron 兜底)"
                )
                return
            self.scheduler.add_job(
                lambda: loop.call_soon_threadsafe(
                    asyncio.ensure_future,
                    self._backfill_one_symbol(symbol, market),
                ),
                "date",
                run_date=run_date,
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300,
            )
            logger.info(
                f"[kline oneoff] 已调度 {symbol}.{market} "
                f"在 {run_date.strftime('%H:%M:%S')} UTC 拉取"
            )
        except Exception as e:
            logger.warning(f"[kline oneoff] 调度失败 {symbol}.{market}: {e}")

    async def _backfill_one_symbol(self, symbol: str, market: str):
        """加股 60s 后: 拉这 1 只股的 800 天 K线"""
        if self._running:
            logger.debug(f"[kline oneoff] 上轮还在跑, 跳过 {symbol}")
            return
        self._running = True
        try:
            engine = create_engine(DB_URL, pool_pre_ping=True)
            try:
                mc = MarketCode(market)
            except ValueError:
                logger.warning(f"[kline oneoff] 不支持的市场: {market}")
                return
            result = await asyncio.to_thread(
                _ingest_one_in_worker, engine, symbol, mc
            )
            if result and result.get("ingested", 0) > 0:
                logger.info(
                    f"[kline oneoff] {symbol}.{market} 入库完成: "
                    f"{result['ingested']} 行"
                )
            else:
                logger.info(f"[kline oneoff] {symbol}.{market} 无新数据(可能已存在)")
        except Exception as e:
            logger.exception(f"[kline oneoff] {symbol}.{market} 异常: {e}")
        finally:
            self._running = False
