"""marketdata 的 Redis 共享缓存后端(批次1-B, 2026-09-10)。

**要解决的问题**: `marketdata` 的 Engine 用**进程内** `TTLCache`, 而生产 `WEB_WORKERS=2`
→ 同一标的会被两个 worker **各拉一次**(缓存互不可见)。此处提供一个**跨进程共享**的
`CacheBackend` 实现, 由宿主注入 `MarketData(cache_factory=...)`。

设计要点:
- **同步** `redis.Redis`: Engine.fetch 是同步路径, 不复用 `src/db/redis_client` 的
  asyncio 客户端(避免跨事件循环 "attached to a different loop")。
- 值 = `marketdata.types` 里的 dataclass → JSON(`__cls__` 标记 + 递归), 解码按类名回建。
  **编解码失败一律当未命中(回源), 绝不返回错值**; 类型不在 `marketdata.types` 命名空间
  则编码失败 → 不缓存。
- **故障回退**: Redis 不可用/超时 → 自动退回进程内 `TTLCache`(即改造前行为),
  绝不退化成"没有缓存"(那会造成打源风暴)。失败后 60s 重试恢复。
- TTL 加 **±10% 抖动**, 避免同批 key 同时过期。
- 计数(hit/miss/err/fallback)供观测。
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from typing import Any

from marketdata import types as md_types
from marketdata.cache import TTLCache

logger = logging.getLogger(__name__)

_KEY_PREFIX = "md:"
_MAX_VALUE_BYTES = 2 * 1024 * 1024  # 单值上限, 超限不缓存(避免 Redis 巨值)
_RETRY_AFTER_SEC = 60.0


def redis_cache_enabled() -> bool:
    """`MD_REDIS_CACHE`(默认开) —— 置 0/false/off 可一键退回进程内缓存。"""
    v = (os.getenv("MD_REDIS_CACHE", "") or "").strip().lower()
    return v not in ("0", "false", "no", "off")


def _enc(o: Any) -> Any:
    """对象 → JSON 可序列化结构(dataclass 打 `__cls__` 标记)。"""
    if isinstance(o, (datetime, date)):
        return {"__dt__": o.isoformat()}
    if isinstance(o, set):
        return {"__set__": sorted(o, key=str)}
    if is_dataclass(o) and not isinstance(o, type):
        out: dict[str, Any] = {"__cls__": type(o).__name__}
        for f in fields(o):
            out[f.name] = _enc(getattr(o, f.name))
        return out
    if isinstance(o, (list, tuple)):
        return [_enc(v) for v in o]
    if isinstance(o, dict):
        return {k: _enc(v) for k, v in o.items()}
    return o


def _dec(o: Any) -> Any:
    """JSON 结构 → 对象; 未知类型抛错(由调用方转成"未命中")。"""
    if isinstance(o, list):
        return [_dec(v) for v in o]
    if isinstance(o, dict):
        if len(o) == 1 and "__dt__" in o:
            try:
                return datetime.fromisoformat(o["__dt__"])
            except Exception:  # noqa: BLE001
                return o["__dt__"]
        if len(o) == 1 and "__set__" in o:
            return set(o["__set__"])
        name = o.get("__cls__")
        if isinstance(name, str):
            cls = getattr(md_types, name, None)
            if not (cls and is_dataclass(cls)):
                raise ValueError(f"未知类型 {name}")
            return cls(**{k: _dec(v) for k, v in o.items() if k != "__cls__"})
        return {k: _dec(v) for k, v in o.items()}
    return o


class RedisTTLCache:
    """实现 `marketdata.cache.CacheBackend`; Redis 故障时回退进程内 TTLCache。"""

    _client: Any = None
    _client_fail_ts: float = 0.0

    def __init__(self, default_ttl_sec: float = 5.0):
        self._ttl = default_ttl_sec
        self._fallback = TTLCache(default_ttl_sec=default_ttl_sec)
        self.stats = {"hit": 0, "miss": 0, "err": 0, "local": 0}

    # -- Redis 连接(失败后 60s 重试) --------------------------------------
    @classmethod
    def _get_client(cls) -> Any:
        if cls._client is not None:
            return cls._client
        if cls._client_fail_ts and (time.time() - cls._client_fail_ts) < _RETRY_AFTER_SEC:
            return None
        url = (os.getenv("REDIS_URL") or "").strip()
        if not url:
            cls._client_fail_ts = time.time()
            return None
        try:
            import redis  # 同步客户端(Engine.fetch 是同步路径)

            cli = redis.Redis.from_url(
                url,
                socket_connect_timeout=0.3,
                socket_timeout=0.3,
                decode_responses=True,
            )
            cli.ping()
            cls._client = cli
            logger.info("[md-cache] Redis 共享缓存启用: %s", url)
            return cli
        except Exception as e:  # noqa: BLE001
            cls._client = None
            cls._client_fail_ts = time.time()
            logger.warning("[md-cache] Redis 不可用, 退回进程内缓存(60s 后重试): %s", e)
            return None

    # -- CacheBackend ------------------------------------------------------
    def get(self, key: str) -> Any | None:
        if self._ttl <= 0:
            return None
        cli = self._get_client()
        if cli is None:
            self.stats["local"] += 1
            return self._fallback.get(key)
        try:
            raw = cli.get(_KEY_PREFIX + key)
        except Exception:  # noqa: BLE001
            self.stats["err"] += 1
            return self._fallback.get(key)
        if raw is None:
            self.stats["miss"] += 1
            return None
        try:
            val = _dec(json.loads(raw))
        except Exception:  # noqa: BLE001 - 解不出来就当未命中, 绝不返回错值
            self.stats["err"] += 1
            return None
        self.stats["hit"] += 1
        return val

    def set(self, key: str, value: Any, ttl_sec: float | None = None) -> None:
        ttl = self._ttl if ttl_sec is None else ttl_sec
        if ttl <= 0:
            return
        ttl *= random.uniform(0.9, 1.1)  # 抖动, 防同批过期
        cli = self._get_client()
        try:
            payload = json.dumps(_enc(value), ensure_ascii=False, separators=(",", ":"))
        except Exception:  # noqa: BLE001 - 编不出来就不缓存(下次仍未命中, 不会错)
            self.stats["err"] += 1
            return
        if len(payload) > _MAX_VALUE_BYTES:
            return
        if cli is None:
            self._fallback.set(key, value, ttl_sec=ttl)
            return
        try:
            cli.setex(_KEY_PREFIX + key, int(max(1, ttl)), payload)
        except Exception:  # noqa: BLE001
            self.stats["err"] += 1
            self._fallback.set(key, value, ttl_sec=ttl)

    def clear(self) -> None:
        self._fallback.clear()

    def __len__(self) -> int:
        return len(self._fallback)


def redis_cache_factory() -> Any:
    """注入给 `MarketData(cache_factory=...)`; 未启用返回 None(包内默认进程内缓存)。"""
    if not redis_cache_enabled():
        logger.info("[md-cache] MD_REDIS_CACHE=0, 保持进程内缓存")
        return None
    return lambda ttl: RedisTTLCache(default_ttl_sec=ttl)
