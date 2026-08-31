"""K线盘前预缓存(v0.4.10)。

工作日 09:20(集合竞价后、开盘前)主动把「自选 + 候选池当日」全部标的的
日K拉一遍入库(增量, 幂等), 让开盘后所有消费者(自选页/机会页/预测/AI)
直接命中 PG 缓存, 对外请求数砍掉 ~80%, 从源头降低触发数据源风控的概率。

注册方式对齐 auction_pool.register_cron: 复用现有 APScheduler 实例。
"""
import logging

logger = logging.getLogger(__name__)


def register_precache_cron(scheduler) -> bool:
    """注册工作日 09:20 盘前 K线预缓存 job 到传入的 APScheduler。"""
    if scheduler is None or not hasattr(scheduler, "add_job"):
        return False

    def _precache_once():
        try:
            import asyncio

            from src.collectors.klines_ingestor import (
                get_default_symbols,
                ingest_symbol,
            )
            from src.web.database import engine

            symbols = get_default_symbols()
            if not symbols:
                logger.info("[kline-precache] 无标的, 跳过")
                return
            logger.info("[kline-precache] 开始盘前预缓存: %d 只", len(symbols))

            async def _run():
                total = 0
                ok = 0
                for sym, mkt in symbols:
                    try:
                        market = __import__(
                            "src.models.market", fromlist=["MarketCode"]
                        ).MarketCode(mkt)
                        stat = await ingest_symbol(
                            engine, sym, market, "1d", 5
                        )  # 增量: 只要最近几天
                        total += stat.get("ingested", 0) if isinstance(stat, dict) else 0
                        ok += 1
                    except Exception as e:  # noqa: BLE001
                        logger.debug("[kline-precache] %s 失败: %r", sym, e)
                logger.info(
                    "[kline-precache] 完成: %d/%d 只, 入库 %d 行", ok, len(symbols), total
                )

            asyncio.run(_run())
        except Exception as e:  # noqa: BLE001
            logger.error("[kline-precache] 预缓存失败(不影响交易时段): %r", e)

    try:
        scheduler.add_job(
            _precache_once,
            "cron",
            day_of_week="mon-fri",
            hour=9,
            minute=20,
            id="kline_precache_morning",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info("[kline-precache] 盘前预缓存 cron 已注册: 工作日 09:20")
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("[kline-precache] cron 注册失败: %r", e)
        return False
