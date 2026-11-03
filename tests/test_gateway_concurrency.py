# -*- coding: utf-8 -*-
"""Gateway 并发优化: 令牌桶 + 热点缓存。"""
import time
import pytest
from unittest.mock import MagicMock, patch

from src.web.api import skills_gateway as gw


def test_token_bucket_burst_then_throttle():
    gw._TOKEN_BUCKET.clear()
    gw._MEM_DAY.clear()
    with patch.object(gw, "_redis_client", return_value=None):
        # burst 30, refill 15/min → 前 30 次应通过
        for i in range(30):
            gw._check_rate_limit("tok1", daily_limit=1000, tier="free")
        # 第 31 次应 429 (桶空)
        with pytest.raises(Exception) as ei:
            gw._check_rate_limit("tok1", daily_limit=1000, tier="free")
        assert "429" in str(ei.value) or "频率" in str(ei.value)


def test_token_bucket_refills():
    gw._TOKEN_BUCKET.clear()
    gw._MEM_DAY.clear()
    with patch.object(gw, "_redis_client", return_value=None):
        # 耗尽
        for _ in range(30):
            gw._check_rate_limit("tok2", daily_limit=1000, tier="free")
        # 手动把桶设为有 1 个 token
        with gw._TB_LOCK:
            gw._TOKEN_BUCKET["tok2"] = (1.0, time.time())
        gw._check_rate_limit("tok2", daily_limit=1000, tier="free")  # 应通过


def test_daily_limit_still_enforced():
    gw._TOKEN_BUCKET.clear()
    gw._MEM_DAY.clear()
    with patch.object(gw, "_redis_client", return_value=None):
        with pytest.raises(Exception) as ei:
            for _ in range(5):
                gw._check_rate_limit("tok3", daily_limit=3, tier="pro")
        assert "配额" in str(ei.value) or "429" in str(ei.value)


class _FakeBizCache:
    def __init__(self):
        self.data, self.ttls = {}, {}

    def get_json(self, key):
        return self.data.get(key)

    def set_json(self, key, value, ttl=None):
        self.data[key] = value
        self.ttls[key] = ttl
        return True


def test_hot_cache_roundtrip(monkeypatch):
    """2026-11-03: 热点缓存委托给 biz_cache(L1+L2), 不再自带进程内 dict。

    原实现只有进程内 dict + 4s TTL, 且 key 用 `hash(frozenset(args))`(每进程随机化),
    跨 worker 永不命中 —— 见 tests/test_skill_gateway_cache_and_ip_limit.py。
    """
    fake = _FakeBizCache()
    monkeypatch.setattr(gw, "biz_cache", fake)
    key = gw._run_cache_key("get_stock_quote", "json", {"symbol": "600519"})
    assert gw._hot_cache_get(key) is None
    gw._hot_cache_set(key, {"skill": "get_stock_quote"})
    assert gw._hot_cache_get(key) == {"skill": "get_stock_quote"}
    assert fake.ttls[key] == 180, "TTL 必须是 3 分钟"


def test_hot_cache_层异常不抛(monkeypatch):
    class _Boom:
        def get_json(self, key):
            raise RuntimeError("redis down")

        def set_json(self, key, value, ttl=None):
            raise RuntimeError("redis down")

    monkeypatch.setattr(gw, "biz_cache", _Boom())
    assert gw._hot_cache_get("x") is None
    gw._hot_cache_set("x", {"a": 1})  # 不抛


def test_retry_after_header_in_429():
    gw._TOKEN_BUCKET.clear()
    gw._MEM_DAY.clear()
    with patch.object(gw, "_redis_client", return_value=None):
        for _ in range(30):
            gw._check_rate_limit("tok4", daily_limit=1000, tier="free")
        with pytest.raises(Exception) as ei:
            gw._check_rate_limit("tok4", daily_limit=1000, tier="free")
        # HTTPException 有 headers
        e = ei.value
        assert hasattr(e, "headers") and e.headers.get("Retry-After")
