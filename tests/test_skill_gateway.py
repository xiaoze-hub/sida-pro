# -*- coding: utf-8 -*-
"""Skill Gateway Phase 1 单测(2026-09-15)。"""
import pytest
from unittest.mock import MagicMock, patch

from src.web.api import skills_gateway as gw



def _req_from(ip: str):
    """构造带真实 IP 的请求桩。

    2026-11-03 起限流按**XFF 感知**的真实 IP 计数(nginx 后面 client.host 恒为 127.0.0.1),
    所以测试必须像反代那样给出 x-forwarded-for, 而不是只改 client.host。
    """
    req = MagicMock()
    req.client.host = "127.0.0.1"  # 可信反代
    req.headers.get = lambda k, d=None: ip if str(k).lower() == "x-forwarded-for" else d
    return req


def _clear_issue_limit(ip: str) -> None:
    from src.web.cache.biz_cache import biz_cache

    biz_cache.delete(f"{gw._KEY_ISSUE_IP_KEY}:{ip}")

def test_open_skills_not_blocked():
    assert not (set(gw.OPEN_SKILLS) & gw.BLOCKED_SKILLS)
    assert "get_stock_quote" in gw.OPEN_SKILLS
    assert "get_portfolio" not in gw.OPEN_SKILLS


def test_hash_key_stable():
    a = gw._hash_key("sk_test")
    b = gw._hash_key("sk_test")
    assert a == b
    assert len(a) == 64  # sha256 hex


def test_rate_limit_memory_fallback():
    gw._MEM_DAY.clear()
    gw._TOKEN_BUCKET.clear()
    with patch.object(gw, "_redis_client", return_value=None):
        # burst 30, 日限 5 → 前 5 次通过
        for _ in range(5):
            gw._check_rate_limit("k1", daily_limit=5, tier="free")
        # 日限触发
        with pytest.raises(Exception) as ei:
            gw._check_rate_limit("k1", daily_limit=5, tier="free")
        assert "429" in str(ei.value) or "配额" in str(ei.value)


def test_check_skill_tier_blocked():
    row = MagicMock()
    row.tier = "pro"
    with pytest.raises(Exception) as ei:
        gw._check_skill_tier("get_portfolio", row)
    assert "不对外开放" in str(ei.value)


def test_check_skill_tier_upgrade_required():
    row = MagicMock()
    row.tier = "free"
    with pytest.raises(Exception) as ei:
        gw._check_skill_tier("get_orderbook", row)  # needs pro
    assert "pro" in str(ei.value)


def test_check_skill_tier_ok():
    row = MagicMock()
    row.tier = "trial"
    gw._check_skill_tier("get_stock_quote", row)  # no raise


def test_register_key_returns_raw_once():
    db = MagicMock()
    row = MagicMock()
    row.key_prefix = "sk_abcd"
    row.tier = "trial"
    row.daily_limit = 500
    row.expires_at = None

    class FakeQuery:
        def filter(self, *a, **k):
            return self
        def first(self):
            return None

    # 2026-11-03: 领 key 按真实 IP 限流(同 IP 30 分钟 1 把)
    ip = "203.0.113.7"
    req = _req_from(ip)
    _clear_issue_limit(ip)

    with patch.object(gw.SkillApiKey, "__init__", return_value=None):
        with patch.object(gw.db, "add") if False else patch("src.web.api.skills_gateway.SkillApiKey") as MockKey:
            inst = MagicMock()
            inst.key_prefix = "sk_abcd"
            inst.tier = "trial"
            inst.daily_limit = 500
            inst.expires_at = None
            MockKey.return_value = inst
            out = gw.register_key(gw.KeyRegisterRequest(owner_label="test"), request=req, db=db)
    assert out["api_key"].startswith("sk_")
    assert out["tier"] == "trial"
    assert out["daily_limit"] == 500
    assert "明文" in out["note"] or "保存" in out["note"]
    assert out["risk"]


def test_register_key_ip_rate_limit():
    """2026-11-03: 同 IP 30 分钟内只能领 1 把 key, 第 2 次 429。

    为什么从"每小时 5 次"收紧: 实测攻击者一把 key 被冻后立刻换 IP 领新 key 继续打,
    5 次/小时足够刷出 5 把; 而正常访客一次会话只需要 1 把。
    """
    from fastapi import HTTPException

    ip = "203.0.113.99"
    _clear_issue_limit(ip)
    req = _req_from(ip)
    db = MagicMock()
    with patch("src.web.api.skills_gateway.SkillApiKey") as MockKey:
        inst = MagicMock()
        inst.key_prefix = "sk_x"
        inst.tier = "free"
        inst.daily_limit = 100
        inst.expires_at = None
        MockKey.return_value = inst
        gw.register_key(gw.KeyRegisterRequest(owner_label="t"), request=req, db=db)
        with pytest.raises(HTTPException) as ei:
            gw.register_key(gw.KeyRegisterRequest(owner_label="t"), request=req, db=db)
        assert ei.value.status_code == 429
        assert int(ei.value.headers["Retry-After"]) > 0
    _clear_issue_limit(ip)
