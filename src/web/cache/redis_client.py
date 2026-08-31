"""Redis 客户端单例 (2026-08-17 v0.2.65 — Phase 1 架构升级)

架构定位:
- 缓存层 (替代内存 dict, 支持跨进程)
- 限流 token bucket 后端
- Redis Streams 任务队列 (替代部分 APScheduler 职责)

优雅降级:
- Redis 不可达时, 限流回退内存 (in-process dict)
- 缓存不可用时, 应用层 fallback 到源数据
- /health 端点显示 Redis 状态

环境变量:
- REDIS_URL (默认 redis://localhost:6379/0)
- REDIS_DISABLED (默认 false, 设 true 完全禁用 Redis)
"""

import logging
import os
import json
import asyncio
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0").strip()
REDIS_DISABLED = _env_bool("REDIS_DISABLED", False)


class RedisClient:
    """Redis 客户端单例 + 降级策略"""

    _instance: Optional["RedisClient"] = None
    _lock = asyncio.Lock() if False else None  # Python 3.10+ 不需要

    def __init__(self):
        self._client: Any = None  # redis.asyncio.Redis 实例
        self._enabled = not REDIS_DISABLED
        self._healthy = False
        self._last_check: float = 0.0

    @classmethod
    def instance(cls) -> "RedisClient":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def connect(self) -> bool:
        """初始化 Redis 连接, 不可达返回 False (但 instance 仍可工作)"""
        if not self._enabled:
            logger.info("[Redis] REDIS_DISABLED=true, 跳过连接")
            return False
        try:
            # Lazy import (避免 redis 未装时导入失败)
            from redis.asyncio import from_url as redis_from_url
            self._client = redis_from_url(
                REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
            )
            await self._client.ping()
            self._healthy = True
            logger.info(f"[Redis] 连接成功: {REDIS_URL}")
            return True
        except Exception as e:
            self._healthy = False
            self._client = None
            logger.warning(f"[Redis] 连接失败 ({REDIS_URL}): {e}")
            return False

    async def close(self):
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    @property
    def enabled(self) -> bool:
        return self._enabled and self._client is not None

    async def ping(self) -> bool:
        """健康检查 (用于 /health 端点)"""
        if not self._client:
            return False
        try:
            await asyncio.wait_for(self._client.ping(), timeout=1.0)
            self._healthy = True
            return True
        except Exception:
            self._healthy = False
            return False

    # ─── 缓存 helper ───
    async def get(self, key: str) -> Optional[str]:
        if not self.enabled:
            return None
        try:
            # decode_responses=True 已设, 返回 str
            result = await self._client.get(key)
            return str(result) if result is not None else None
        except Exception as e:
            logger.warning(f"[Redis] get({key}) failed: {e}")
            return None

    async def set(self, key: str, value: str, ttl_seconds: Optional[int] = None) -> bool:
        if not self.enabled:
            return False
        try:
            if ttl_seconds:
                await self._client.setex(key, ttl_seconds, value)
            else:
                await self._client.set(key, value)
            return True
        except Exception as e:
            logger.warning(f"[Redis] set({key}) failed: {e}")
            return False

    async def delete(self, *keys: str) -> int:
        if not self.enabled or not keys:
            return 0
        try:
            return int(await self._client.delete(*keys))
        except Exception as e:
            logger.warning(f"[Redis] delete failed: {e}")
            return 0

    async def incr(self, key: str, ttl_seconds: Optional[int] = None) -> Optional[int]:
        """原子递增 — 用于限流计数器"""
        if not self.enabled:
            return None
        try:
            pipe = self._client.pipeline()
            pipe.incr(key)
            if ttl_seconds:
                pipe.expire(key, ttl_seconds)
            results = await pipe.execute()
            return int(results[0]) if results else None
        except Exception as e:
            logger.warning(f"[Redis] incr({key}) failed: {e}")
            return None

    async def stream_add(self, stream: str, data: dict, maxlen: int = 10000) -> Optional[str]:
        """XADD 到 stream, 满了截断到 maxlen"""
        if not self.enabled:
            return None
        try:
            result = await self._client.xadd(stream, data, maxlen=maxlen, approximate=True)
            return str(result) if result else None
        except Exception as e:
            logger.warning(f"[Redis] xadd({stream}) failed: {e}")
            return None

    # ─── JSON helper ───
    async def get_json(self, key: str) -> Optional[Any]:
        s = await self.get(key)
        if s is None:
            return None
        try:
            return json.loads(s)
        except Exception:
            return None

    async def set_json(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> bool:
        try:
            return await self.set(key, json.dumps(value, default=str), ttl_seconds)
        except Exception:
            return False

    # ─── Redis Streams (Phase 1 任务队列) ───
    async def stream_len(self, stream: str) -> int:
        if not self.enabled:
            return 0
        try:
            return await self._client.xlen(stream)
        except Exception:
            return 0

    async def stream_groups(self, stream: str) -> int:
        if not self.enabled:
            return 0
        try:
            groups = await self._client.xinfo_groups(stream)
            return len(groups)
        except Exception:
            return 0


# 全局单例
redis_client = RedisClient.instance()