"""批次1-B(2026-09-10): marketdata Redis 共享缓存回归。

背景: Engine 原先用**进程内** TTLCache, 而生产 `WEB_WORKERS=2` → 同标的被两个 worker
各拉一次。这里验证: ① dataclass ↔ JSON 编解码可逆; ② Redis 可用时读写走 Redis;
③ **Redis 不可用/解码失败时降级安全**(退回进程内 / 当未命中), 绝不返回错值;
④ `MarketData(cache_factory=...)` 注入对所有 Engine 生效。
"""
from __future__ import annotations

import time

import pytest

from marketdata.cache import TTLCache
from marketdata.client import MarketData
from marketdata.types import Bar, CapitalFlow, Quote
from src.core import md_redis_cache as mrc


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def ping(self):
        return True

    def setex(self, k, ttl, v):
        self.store[k] = v
        return True

    def get(self, k):
        return self.store.get(k)


@pytest.fixture()
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(mrc.RedisTTLCache, "_client", fake)
    monkeypatch.setattr(mrc.RedisTTLCache, "_client_fail_ts", 0.0)
    return fake


def test_codec_roundtrip_dataclasses():
    bars = [Bar(date="2026-09-10", open=1.0, close=2.0, high=2.5, low=0.9, volume=100.0)]
    quotes = [Quote(symbol="600519", market="CN", current_price=1288.5, change_pct=-1.2)]
    flows = [CapitalFlow(symbol="600519", name="茅台", main_net_inflow=-1e8, date="2026-09-09")]
    for val in (bars, quotes, flows):
        assert mrc._dec(mrc._enc(val)) == val


def test_redis_roundtrip_and_hit_stat(fake_redis):
    c = mrc.RedisTTLCache(default_ttl_sec=5.0)
    bars = [Bar(date="2026-09-10", open=1.0, close=2.0, high=2.5, low=0.9)]
    c.set("k1", bars)
    assert "md:k1" in fake_redis.store  # 写进了 Redis(带前缀)
    assert c.get("k1") == bars
    assert c.stats["hit"] == 1
    assert c.get("nope") is None
    assert c.stats["miss"] == 1


def test_decode_failure_is_treated_as_miss(fake_redis):
    fake_redis.store["md:bad"] = '{"__cls__":"NoSuchTypeHere"}'
    c = mrc.RedisTTLCache(default_ttl_sec=5.0)
    assert c.get("bad") is None          # 解不出来 → 当未命中(回源), 不返回错值
    assert c.stats["err"] >= 1


def test_ttl_zero_does_not_cache():
    c = mrc.RedisTTLCache(default_ttl_sec=0.0)
    c.set("k", [1, 2, 3])
    assert c.get("k") is None


def test_falls_back_to_local_when_redis_unavailable(monkeypatch):
    monkeypatch.setattr(mrc.RedisTTLCache, "_client", None)
    monkeypatch.setattr(mrc.RedisTTLCache, "_client_fail_ts", time.time())  # 抑制重连
    monkeypatch.delenv("REDIS_URL", raising=False)
    c = mrc.RedisTTLCache(default_ttl_sec=5.0)
    bars = [Bar(date="2026-09-10", open=1.0, close=2.0, high=2.5, low=0.9)]
    c.set("k", bars)
    assert c.get("k") == bars            # 退回进程内, 行为同改造前
    assert c.stats["local"] >= 1


def test_unencodable_value_is_not_cached(monkeypatch):
    class _Weird:  # 非 dataclass 且含不可 JSON 化对象
        pass

    monkeypatch.setattr(mrc.RedisTTLCache, "_client", None)
    monkeypatch.setattr(mrc.RedisTTLCache, "_client_fail_ts", time.time())
    monkeypatch.delenv("REDIS_URL", raising=False)
    c = mrc.RedisTTLCache(default_ttl_sec=5.0)
    c.set("k", [object()])               # 编码失败 → 不缓存, 但不得抛
    assert c.get("k") is None


def test_enabled_flag(monkeypatch):
    monkeypatch.setenv("MD_REDIS_CACHE", "0")
    assert mrc.redis_cache_enabled() is False
    assert mrc.redis_cache_factory() is None
    monkeypatch.setenv("MD_REDIS_CACHE", "1")
    assert mrc.redis_cache_enabled() is True


def test_marketdata_uses_injected_cache_factory():
    seen: list[float] = []

    def factory(ttl: float):
        seen.append(ttl)
        return TTLCache(default_ttl_sec=ttl)

    MarketData(config=object(), cache_factory=factory)  # type: ignore[arg-type]
    assert len(seen) >= 12               # 所有 Engine 都走工厂
    assert 5.0 in seen and 300.0 in seen and 0.0 in seen
