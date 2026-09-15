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
    gw._MEM_MIN.clear()
    with patch.object(gw, "_redis_client", return_value=None):
        # 3 次成功
        for _ in range(3):
            gw._check_rate_limit("k1", daily_limit=5, minute_limit=10)
        # 第 4 次仍 ok
        gw._check_rate_limit("k1", daily_limit=5, minute_limit=10)
        # 日限
        with pytest.raises(Exception) as ei:
            for _ in range(5):
                gw._check_rate_limit("k1", daily_limit=4, minute_limit=10)
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

    with patch.object(gw.SkillApiKey, "__init__", return_value=None):
        with patch.object(gw.db, "add") if False else patch("src.web.api.skills_gateway.SkillApiKey") as MockKey:
            inst = MagicMock()
            inst.key_prefix = "sk_abcd"
            inst.tier = "trial"
            inst.daily_limit = 500
            inst.expires_at = None
            MockKey.return_value = inst
            out = gw.register_key(gw.KeyRegisterRequest(owner_label="test"), db=db)
    assert out["api_key"].startswith("sk_")
    assert out["tier"] == "trial"
    assert out["daily_limit"] == 500
    assert "明文" in out["note"] or "保存" in out["note"]
    assert out["risk"]
