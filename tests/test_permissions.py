# -*- coding: utf-8 -*-
"""用户权限体系单测(2026-09-15)。"""
import pytest
from unittest.mock import MagicMock, patch

from src.core import permissions as perm


def test_normalize_role():
    assert perm.normalize_role("owner") == "owner"
    assert perm.normalize_role("admin") == "owner"
    assert perm.normalize_role("pro") == "pro"
    assert perm.normalize_role("member") == "member"
    assert perm.normalize_role(None) == "member"
    assert perm.normalize_role("weird") == "member"


def test_member_can_view_quote_heatmap_portfolio():
    u = MagicMock()
    u.role = "member"
    assert perm.has_perm(u, perm.PERM_VIEW_QUOTE)
    assert perm.has_perm(u, perm.PERM_VIEW_HEATMAP)
    assert perm.has_perm(u, perm.PERM_EDIT_PORTFOLIO)


def test_member_cannot_view_locked():
    u = MagicMock()
    u.role = "member"
    assert not perm.has_perm(u, perm.PERM_VIEW_OPPORTUNITIES)
    assert not perm.has_perm(u, perm.PERM_VIEW_L2)
    assert not perm.has_perm(u, perm.PERM_VIEW_DARK)
    assert not perm.has_perm(u, perm.PERM_MANAGE_SYSTEM)


def test_pro_can_view_all_locked():
    u = MagicMock()
    u.role = "pro"
    assert perm.has_perm(u, perm.PERM_VIEW_OPPORTUNITIES)
    assert perm.has_perm(u, perm.PERM_VIEW_L2)
    assert not perm.has_perm(u, perm.PERM_MANAGE_SYSTEM)


def test_owner_can_manage_system():
    u = MagicMock()
    u.role = "owner"
    assert perm.has_perm(u, perm.PERM_MANAGE_SYSTEM)
    assert perm.has_perm(u, perm.PERM_MANAGE_USERS)


def test_guest_no_perms():
    u = None
    assert not perm.has_perm(u, perm.PERM_VIEW_QUOTE)
    g = MagicMock()
    g.role = "guest"
    assert not perm.has_perm(g, perm.PERM_VIEW_QUOTE)


def test_trial_quota_3_times():
    perm._TRIAL_MEM.clear()
    u = MagicMock()
    u.id = "user_trial"
    u.role = "member"
    db = MagicMock()
    # 3 次试用通过
    for i in range(3):
        with patch("src.core.permissions._trial_used", return_value=i):
            with patch("src.core.permissions._trial_incr"):
                perm.enforce_perm(u, perm.PERM_VIEW_OPPORTUNITIES, db)
    # 第 4 次 403
    with patch("src.core.permissions._trial_used", return_value=3):
        with pytest.raises(Exception) as ei:
            perm.enforce_perm(u, perm.PERM_VIEW_OPPORTUNITIES, db)
        assert "403" in str(ei.value) or "试用" in str(ei.value)


def test_member_watchlist_quota():
    u = MagicMock()
    u.id = "user_wl"
    u.role = "member"
    db = MagicMock()
    q = MagicMock()
    q.filter.return_value.count.return_value = 10
    db.query.return_value = q
    with pytest.raises(Exception) as ei:
        perm.check_watchlist_quota(db, u)
    assert "上限" in str(ei.value)


def test_pro_watchlist_no_limit():
    u = MagicMock()
    u.id = "user_pro"
    u.role = "pro"
    db = MagicMock()
    # 不应抛
    perm.check_watchlist_quota(db, u)
