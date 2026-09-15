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


def test_hot_cache_roundtrip():
    gw._HOT_CACHE.clear()
    key = "skill_cache:test:{'symbol': '600519'}"
    assert gw._hot_cache_get(key) is None
    resp = MagicMock()
    gw._hot_cache_set(key, resp)
    assert gw._hot_cache_get(key) is resp


def test_hot_cache_expiry():
    gw._HOT_CACHE.clear()
    key = "skill_cache:test2:{'symbol': '000001'}"
    gw._hot_cache_set(key, "val")
    # 手动改过期
    with gw._HOT_LOCK:
        gw._HOT_CACHE[key] = (time.time() - 10, "val")
    assert gw._hot_cache_get(key) is None


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
