# -*- coding: utf-8 -*-
"""统一身份(2026-09-16): 游客试用 + web 计量。"""
import time
import pytest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from src.web.api import skills_gateway as gw


def test_guest_rate_limit_allows_then_429():
    ip = "198.51.100.7"
    gw._guest_ip_counts.pop(ip, None)
    for _ in range(gw.GUEST_DAILY_LIMIT):
        gw._check_guest_rate(ip)
    with pytest.raises(HTTPException) as ei:
        gw._check_guest_rate(ip)
    assert ei.value.status_code == 429
    assert "请注册获取 API Key" in str(ei.value.detail)
    gw._guest_ip_counts.pop(ip, None)


def test_guest_rate_limit_window_slides():
    ip = "198.51.100.8"
    gw._guest_ip_counts.pop(ip, None)
    # 塞满窗口, 然后把最早一条挪出 24h
    now = time.monotonic()
    gw._guest_ip_counts[ip] = [now - gw._GUEST_WINDOW_SEC - 1] + [
        now - i for i in range(gw.GUEST_DAILY_LIMIT - 1)
    ]
    gw._check_guest_rate(ip)  # 应通过(旧记录滑出窗口)
    gw._guest_ip_counts.pop(ip, None)


def test_guest_rate_limit_thread_safe_lock():
    assert gw._guest_lock is not None
    # 简单并发写: 不抛异常即可
    ip = "198.51.100.9"
    gw._guest_ip_counts.pop(ip, None)
    import threading

    errors: list[BaseException] = []

    def _hit():
        try:
            gw._check_guest_rate(ip)
        except HTTPException:
            pass
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_hit) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(gw._guest_ip_counts.get(ip, [])) <= gw.GUEST_DAILY_LIMIT
    gw._guest_ip_counts.pop(ip, None)


def test_check_call_tier_guest_blocks_pro():
    ident = gw._CallIdentity(
        channel="guest",
        api_key_id=0,
        user_id=None,
        rate_key="guest:1.2.3.4",
        tier="free",
        daily_limit=gw.GUEST_DAILY_LIMIT,
        key_row=None,
    )
    with pytest.raises(HTTPException) as ei:
        gw._check_call_tier("get_forecast", ident)  # pro
    assert ei.value.status_code == 403
    # free 档应放行
    gw._check_call_tier("get_stock_quote", ident)


def test_check_call_tier_api_path_unchanged():
    row = MagicMock()
    row.tier = "free"
    ident = gw._CallIdentity(
        channel="api",
        api_key_id=1,
        user_id=None,
        rate_key="hash",
        tier="free",
        daily_limit=100,
        key_row=row,
    )
    with pytest.raises(HTTPException):
        gw._check_call_tier("get_orderbook", ident)  # needs pro
    ident_pro = gw._CallIdentity(
        channel="api",
        api_key_id=1,
        user_id=None,
        rate_key="hash",
        tier="pro",
        daily_limit=5000,
        key_row=row,
    )
    gw._check_call_tier("get_orderbook", ident_pro)  # no raise


def test_resolve_identity_api_key_wins():
    db = MagicMock()
    row = MagicMock()
    row.id = 42
    row.user_id = "u-1"
    row.key_hash = "hash-42"
    row.tier = "trial"
    row.daily_limit = 500
    req = MagicMock()
    req.client.host = "10.0.0.1"
    with patch.object(gw, "_validate_api_key_row", return_value=row):
        ident = gw._resolve_call_identity("sk_abc", "Bearer whatever", req, db)
    assert ident.channel == "api"
    assert ident.api_key_id == 42
    assert ident.user_id == "u-1"
    assert ident.rate_key == "hash-42"
    assert ident.key_row is row


def test_resolve_identity_jwt_web_shares_quota():
    db = MagicMock()
    user = MagicMock()
    user.id = "user-uuid-1"
    key = MagicMock()
    key.id = 7
    key.key_hash = "shared-hash"
    key.tier = "trial"
    key.daily_limit = 500
    key.status = "active"
    key.expires_at = None
    req = MagicMock()
    req.client.host = "10.0.0.2"
    with patch.object(gw, "_optional_jwt_user", return_value=user):
        with patch.object(gw, "_best_active_key_for_user", return_value=key):
            ident = gw._resolve_call_identity("", "Bearer tok", req, db)
    assert ident.channel == "web"
    assert ident.api_key_id == 7
    assert ident.user_id == "user-uuid-1"
    assert ident.rate_key == "shared-hash"  # 与 API 共享配额
    assert ident.tier == "trial"


def test_resolve_identity_jwt_without_key_free_tier():
    db = MagicMock()
    user = MagicMock()
    user.id = "user-uuid-2"
    req = MagicMock()
    req.client.host = "10.0.0.3"
    with patch.object(gw, "_optional_jwt_user", return_value=user):
        with patch.object(gw, "_best_active_key_for_user", return_value=None):
            ident = gw._resolve_call_identity("", "Bearer tok", req, db)
    assert ident.channel == "web"
    assert ident.api_key_id == 0
    assert ident.rate_key == "web_user:user-uuid-2"
    assert ident.tier == "free"
    assert ident.daily_limit == gw.TIER_DAILY_LIMIT["free"]


def test_resolve_identity_guest():
    db = MagicMock()
    req = MagicMock()
    req.client.host = "10.0.0.4"
    gw._guest_ip_counts.pop("10.0.0.4", None)
    with patch.object(gw, "_optional_jwt_user", return_value=None):
        ident = gw._resolve_call_identity("", "", req, db)
    assert ident.channel == "guest"
    assert ident.api_key_id == 0
    assert ident.user_id is None
    assert ident.tier == "free"
    assert "10.0.0.4" in gw._guest_ip_counts
    gw._guest_ip_counts.pop("10.0.0.4", None)


def test_log_usage_includes_channel_and_user():
    db = MagicMock()
    added = {}
    db.add.side_effect = lambda row: added.update(row.__dict__)
    gw._log_usage(db, 0, "get_stock_quote", 200, 12, channel="guest", user_id=None)
    assert added.get("api_key_id") == 0
    assert added.get("channel") == "guest"
    assert added.get("user_id") is None

    added.clear()
    gw._log_usage(db, 3, "get_stock_quote", 200, 8, channel="web", user_id="u-9")
    assert added.get("channel") == "web"
    assert added.get("user_id") == "u-9"


def test_usage_by_channel_counts():
    db = MagicMock()
    rows = [("api", 1), ("api", 2), ("web", 3), (None, 4), ("guest", 5)]
    q = MagicMock()
    q.filter.return_value.all.return_value = rows
    db.query.return_value = q
    out = gw._usage_by_channel(db, [], __import__("datetime").datetime.now())
    assert out["api"] == 3  # 含 None 归 api
    assert out["web"] == 1
    assert out["guest"] == 1
