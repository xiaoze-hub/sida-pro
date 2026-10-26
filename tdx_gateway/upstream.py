"""上游客户端：单通道串行 + 排队 + 熔断 + 缓存 + 指标。

2026-10-19 实测教训：TdxW 的 17709 服务是单线程的，连续 30 次快请求会直接把整个客户端打崩
（进程都没了，不是只挂 HTTP 线程）。所以这里**默认并发 1**，队列有上限，失败退避，
熔断期间直接返回 503 而不是继续压它。
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import Any

import httpx

from .contract import build_body, profile_for


class Upstream:
    def __init__(self, base_url: str, schema: dict[str, dict], *,
                 concurrency: int = 1, queue_limit: int = 64,
                 fail_threshold: int = 5, open_seconds: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.schema = schema
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._queue_limit = queue_limit
        self._inflight = 0
        self._client: httpx.AsyncClient | None = None
        self._fail_threshold = fail_threshold
        self._open_seconds = open_seconds
        self._open_until = 0.0
        self._fail_streak = 0
        self._cache: dict[str, tuple[float, Any]] = {}
        self._inflight_keys: dict[str, asyncio.Future] = {}
        self.metrics: dict[str, dict[str, float]] = defaultdict(
            lambda: {"ok": 0, "err": 0, "cache_hit": 0, "latency_ms": 0.0})

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def circuit_open(self) -> bool:
        return time.monotonic() < self._open_until

    def _key(self, method: str, params: dict) -> str:
        return f"{method}:{sorted((k, str(v)) for k, v in params.items())}"

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        prof = profile_for(method)
        ttl = float(prof["cache_ttl"])
        key = self._key(method, params)
        if ttl:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < ttl:
                self.metrics[method]["cache_hit"] += 1
                return hit[1]

        if self.circuit_open:
            raise UpstreamUnavailable(
                f"熔断中（最近 {self._fail_threshold} 次失败，{int(self._open_until - time.monotonic())}s 后重试）")
        if self._inflight >= self._queue_limit:
            raise UpstreamBusy(f"队列已满（{self._queue_limit}）—— 17709 单线程，拒绝继续压")

        # single-flight：同一参数的并发请求合并成一次上游调用（客户端单线程，重复打没有意义）
        running = self._inflight_keys.get(key)
        if running is not None:
            return await running

        body = build_body(method, params, self.schema)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._inflight_keys[key] = fut
        self._inflight += 1
        try:
            async with self._sem:                      # 串行闸门
                result = await self._post(method, body, float(prof["timeout"]))
            if ttl and isinstance(result, dict) and str(result.get("ErrorId", "0")) == "0":
                self._cache[key] = (time.time(), result)
            fut.set_result(result)
            return result
        except BaseException as e:
            fut.set_exception(e)
            raise
        finally:
            self._inflight_keys.pop(key, None)
            self._inflight -= 1

    async def _post(self, method: str, body: dict, timeout: float) -> Any:
        assert self._client is not None
        t0 = time.perf_counter()
        try:
            r = await self._client.post(self.base_url, json={"id": 1, "method": method, "params": body},
                                        timeout=timeout)
            r.raise_for_status()
            data = r.json()
        except Exception as e:                          # 传输层失败 → 计失败并可能开熔断
            self._on_failure(method, t0)
            raise UpstreamUnavailable(f"{method} 上游失败: {type(e).__name__}: {e}") from e

        self._fail_streak = 0
        self.metrics[method]["ok"] += 1
        self.metrics[method]["latency_ms"] += (time.perf_counter() - t0) * 1000
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        return data

    def _on_failure(self, method: str, t0: float) -> None:
        self.metrics[method]["err"] += 1
        self.metrics[method]["latency_ms"] += (time.perf_counter() - t0) * 1000
        self._fail_streak += 1
        if self._fail_streak >= self._fail_threshold:
            self._open_until = time.monotonic() + self._open_seconds
            self._fail_streak = 0

    def snapshot_metrics(self) -> dict[str, Any]:
        out = {}
        for method, m in self.metrics.items():
            n = m["ok"] + m["err"]
            out[method] = {
                "ok": int(m["ok"]), "err": int(m["err"]), "cache_hit": int(m["cache_hit"]),
                "avg_ms": round(m["latency_ms"] / n, 1) if n else None,
            }
        return {"upstream": self.base_url, "circuit_open": self.circuit_open,
                "inflight": self._inflight, "methods": out}


class UpstreamUnavailable(RuntimeError):
    pass


class UpstreamBusy(RuntimeError):
    pass
