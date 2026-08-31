"""Redis Streams 任务队列 (2026-08-17 v0.2.65 — Phase 1)

架构定位:
- 替代 APScheduler 部分职责: 跨进程任务分发、崩溃断点续传
- 当前用法: KlineBackfillScheduler 在 ingest_symbol 任务上 push 到 stream
- 未来: 多 worker 进程消费, 横向扩展

Stream 命名:
- stream:tasks:kline_backfill → symbol 级别的 K线回填任务

使用:
- producer: stream_add(stream, {"action": "backfill", "symbol": "000333", "market": "CN", "days": 800})
- consumer: 待定(后续 worker)

当前只示范 producer 接口 + XLEN/XINFO 监控 — 不阻塞 ingest 流程
"""

import logging
from typing import Optional

from src.web.cache.redis_client import redis_client

logger = logging.getLogger(__name__)

# Stream 命名常量
STREAM_KLINE_BACKFILL = "stream:tasks:kline_backfill"


async def publish_kline_backfill(symbol: str, market: str, days: int = 800) -> Optional[str]:
    """发布 K线回填任务到 Redis Stream

    Args:
        symbol: 股票代码(如 '000333')
        market: 市场 (CN / HK / US)
        days: 回填天数

    Returns:
        stream entry id (redis stream msg ID) 或 None(Redis 不可用时静默失败)
    """
    if not redis_client.enabled:
        return None  # 静默降级: Redis 不可用时不影响主流程
    try:
        msg_id = await redis_client.stream_add(
            STREAM_KLINE_BACKFILL,
            {
                "action": "backfill",
                "symbol": symbol,
                "market": market,
                "days": str(days),
                "source": "kline_backfill_scheduler",
            },
            maxlen=10000,
        )
        if msg_id:
            logger.debug(f"[stream] publish backfill task: {symbol}/{market} -> {msg_id}")
        return msg_id
    except Exception as e:
        logger.warning(f"[stream] publish failed: {e}")
        return None


async def get_stream_stats() -> dict:
    """给 /health 端点 — 显示各 stream 长度"""
    if not redis_client.enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "kline_backfill_len": await redis_client.stream_len(STREAM_KLINE_BACKFILL),
        "kline_backfill_groups": await redis_client.stream_groups(STREAM_KLINE_BACKFILL),
    }