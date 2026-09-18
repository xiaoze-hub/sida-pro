"""P2-4 钉子: 公开档位对比接口 —— 免登录、无价格、清单来自权限定义。"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from src.web.app import app

#: 出现这些字段就等于"页面可能显示价格" —— 内测期不收费, 一个都不许有
_PRICE_HINTS = ("price", "amount", "cny", "rmb", "fee", "charge", "cost", "元", "￥", "¥", "usd")


def _get_tiers() -> dict:
    c = TestClient(app)
    r = c.get("/api/tiers")
    assert r.status_code == 200, f"/api/tiers 应免登录可访问, 实际 {r.status_code}"
    return r.json()["data"]


def test_public_without_auth():
    """公开面: 未登录也能拉到 —— 否则落地页/档位页拿不到对比。"""
    body = _get_tiers()
    assert "tiers" in body


def test_no_price_fields_anywhere():
    """**内测期不收费**: 整个响应里不许出现价格字段/金额符号。"""
    raw = json.dumps(_get_tiers(), ensure_ascii=False).lower()
    for hint in _PRICE_HINTS:
        assert hint not in raw, f"响应里出现了 {hint!r} —— 内测期不收费, 不该有任何价格字段"


def test_billing_flag_and_note_are_honest():
    body = _get_tiers()
    assert body["billing_enabled"] is False
    assert "不收费" in body["note"]


def test_only_purchasable_tiers():
    """只列可选档位: guest 权限集是空的(摆一列空的会像坏页), owner 不是可购档位。"""
    keys = [t["key"] for t in _get_tiers()["tiers"]]
    assert keys == ["member", "pro"], f"档位应只有 免费/Pro, 实际 {keys}"


def test_tier_items_come_from_permission_definitions():
    """清单必须是权限定义里真实存在的权限点(不是页面文案抄的)。"""
    from src.core.permissions import PERMISSION_LABELS, get_role_permissions

    for tier in _get_tiers()["tiers"]:
        labels = {PERMISSION_LABELS[k][0] for k in get_role_permissions(tier["key"])}
        listed = {it for g in tier["groups"] for it in g["items"]}
        assert listed == labels, f"{tier['key']} 的条目与权限定义不一致"
        assert tier["count"] == len(labels)


def test_member_limits_are_real_or_absent():
    """免费档上限: 要么给实时值(int), 要么 None + 说明 —— 不许编数字。"""
    body = _get_tiers()
    lim = body["member_limits"]
    if lim is None:
        assert "读不到" in body["limits_note"] or "不可用" in body["limits_note"]
    else:
        assert isinstance(lim["watchlist_max"], int)
        assert isinstance(lim["alert_max"], int)
        assert isinstance(lim["trial_daily_limit"], int)
        assert "实时值" in body["limits_note"]
