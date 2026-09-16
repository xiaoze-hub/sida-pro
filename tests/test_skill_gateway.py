# -*- coding: utf-8 -*-
"""Skill Gateway Phase 1 单测(2026-09-15)。"""
import pytest
from unittest.mock import MagicMock, patch

from src.web.api import skills_gateway as gw


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

    # P1(audit-20260915): register_key 现需要 Request(IP 限流)
    req = MagicMock()
    req.client.host = "127.0.0.1"
    # 独立 IP, 避免与其他用例共享限流窗口
    gw._key_reg_hits.pop("127.0.0.1", None)

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
    """P1: 同 IP 每小时最多 5 次注册, 第 6 次 429。"""
    from fastapi import HTTPException

    ip = "203.0.113.99"
    gw._key_reg_hits.pop(ip, None)
    req = MagicMock()
    req.client.host = ip
    db = MagicMock()
    with patch("src.web.api.skills_gateway.SkillApiKey") as MockKey:
        inst = MagicMock()
        inst.key_prefix = "sk_x"
        inst.tier = "free"
        inst.daily_limit = 100
        inst.expires_at = None
        MockKey.return_value = inst
        for _ in range(5):
            gw.register_key(gw.KeyRegisterRequest(owner_label="t"), request=req, db=db)
        with pytest.raises(HTTPException) as ei:
            gw.register_key(gw.KeyRegisterRequest(owner_label="t"), request=req, db=db)
        assert ei.value.status_code == 429
    gw._key_reg_hits.pop(ip, None)
