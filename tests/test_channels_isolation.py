"""C2(2026-09-08 风险方案1.4)通知渠道跨用户越权回归。

原实现三处洞:
- GET /api/channels 把 config(webhook URL/bot token 明文)整包返回给任何登录者;
- PUT/DELETE 用"自己的+全局"谓词 → 非 owner 可改/删全局渠道;
- POST /{id}/test 完全无鉴权 → 任意用户可对任意渠道触发推送。
对齐 stocks.py 读宽写严: 列表脱敏; 写/测仅本人, 全局仅 owner。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web.api import channels as ch_api
from src.web.models import Base, NotifyChannel, User

from factories import make_notify_channel, make_user

U_OWNER = "cccc1111-0000-0000-0000-000000000001"
U_DEMO = "cccc1111-0000-0000-0000-000000000002"
U_ALICE = "cccc1111-0000-0000-0000-000000000003"

FAKE_PK = "pk1234567890abcdefgh"  # pushplus token 形态(必填字段 token)


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng, tables=[User.__table__, NotifyChannel.__table__])
    S = sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)
    db = S()
    db.add_all([
        make_user(username="owner", id=U_OWNER, role="owner"),
        make_user(username="demo", id=U_DEMO),
        make_user(username="alice", id=U_ALICE),
        make_notify_channel(id=1, user_id=None, name="全局PushPlus", type="pushplus",
                            config={"token": FAKE_PK}),
        make_notify_channel(id=2, user_id=U_ALICE, name="alice的TG", type="telegram",
                            config={"bot_token": "123456:ABC-token", "chat_id": "88001"}),
        make_notify_channel(id=3, user_id=U_DEMO, name="demo的TG", type="telegram",
                            config={"bot_token": "999:demo-token", "chat_id": "88002"},
                            is_default=True),
    ])
    db.commit()
    yield db
    db.close()


def _u(uid: str, role: str = "member"):
    return make_user(id=uid, role=role)


# ── 读: config 脱敏 ──────────────────────────────────────────────


def test_list_config_masked(db):
    """验收: demo 账号 GET /api/channels → 响应不含任何完整 secret。"""
    out = ch_api.list_channels(db=db, user=_u(U_DEMO))
    import json

    blob = json.dumps([o.model_dump() for o in out])
    assert FAKE_PK not in blob
    assert "123456:ABC-token" not in blob
    assert "999:demo-token" not in blob
    # 读宽: demo 可见 全局(1) + 自己的(3); 他人(2)根本不在列表里
    assert {o.id for o in out} == {1, 3}
    g = next(o for o in out if o.id == 1)
    assert g.config["token"].startswith("***") and g.config["token"].endswith("efgh")
    # 非敏感字段不误伤
    own = next(o for o in out if o.id == 3)
    assert own.config["chat_id"] == "88002"


def test_create_returns_masked_but_db_keeps_full(db):
    body = ch_api.ChannelCreate(name="我的tg", type="telegram",
                                config={"bot_token": "777:real-secret-token", "chat_id": "1"})
    out = ch_api.create_channel(body, db=db, user=_u(U_DEMO))
    assert out.config["bot_token"].startswith("***")
    row = db.query(NotifyChannel).filter(NotifyChannel.name == "我的tg").first()
    assert row.config["bot_token"] == "777:real-secret-token"  # 库里必须仍是明文可用


def test_mask_config_unit():
    assert ch_api._mask_config({"bot_token": "short"}) == {"bot_token": "***"}
    assert ch_api._mask_config({"webhook_url": "abcdefghij"}) == {"webhook_url": "***ghij"}
    assert ch_api._mask_config({"chat_id": "88001", "topic": None, "n": 3}) == {
        "chat_id": "88001", "topic": None, "n": 3}
    assert ch_api._mask_config(None) == {}


# ── 写: 读宽写严 ─────────────────────────────────────────────────


def test_demo_cannot_update_global_channel(db):
    """验收: demo PUT 全局渠道 → 403。"""
    body = ch_api.ChannelUpdate(name="被篡改")
    with pytest.raises(HTTPException) as ei:
        ch_api.update_channel(1, body, db=db, user=_u(U_DEMO))
    assert ei.value.status_code == 403


def test_owner_can_update_global_channel(db):
    body = ch_api.ChannelUpdate(name="owner改名")
    out = ch_api.update_channel(1, body, db=db, user=_u(U_OWNER, role="owner"))
    assert out.name == "owner改名"


def test_demo_cannot_touch_others_channel(db):
    with pytest.raises(HTTPException) as ei:
        ch_api.update_channel(2, ch_api.ChannelUpdate(name="x"), db=db, user=_u(U_DEMO))
    assert ei.value.status_code == 403
    with pytest.raises(HTTPException) as ei:
        ch_api.delete_channel(2, db=db, user=_u(U_DEMO))
    assert ei.value.status_code == 403


def test_demo_can_update_own_channel(db):
    out = ch_api.update_channel(3, ch_api.ChannelUpdate(enabled=False), db=db, user=_u(U_DEMO))
    assert out.enabled is False


def test_demo_cannot_delete_global_channel(db):
    with pytest.raises(HTTPException) as ei:
        ch_api.delete_channel(1, db=db, user=_u(U_DEMO))
    assert ei.value.status_code == 403
    assert db.query(NotifyChannel).filter(NotifyChannel.id == 1).first() is not None


def test_owner_can_delete_global_channel(db):
    assert ch_api.delete_channel(1, db=db, user=_u(U_OWNER, role="owner")) == {"ok": True}
    assert db.query(NotifyChannel).filter(NotifyChannel.id == 1).first() is None


def test_missing_channel_404(db):
    with pytest.raises(HTTPException) as ei:
        ch_api.update_channel(99, ch_api.ChannelUpdate(), db=db, user=_u(U_OWNER))
    assert ei.value.status_code == 404


# ── 测试推送端点: 归属校验 ────────────────────────────────────────


def test_demo_cannot_test_global_channel(db):
    """验收: demo 对全局渠道 POST /test → 403(不触达 NotifierManager)。"""
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ch_api.test_channel(1, db=db, user=_u(U_DEMO)))
    assert ei.value.status_code == 403


def test_owner_can_test_global_channel(db, monkeypatch):
    sent: dict = {}

    class _FakeNotifier:
        def add_channel(self, type_, config):
            sent["config"] = config

        async def notify_with_result(self, **kwargs):
            sent["kwargs"] = kwargs
            return {"success": True, "channels": [{"type": "wecom"}]}

    monkeypatch.setattr(ch_api, "NotifierManager", _FakeNotifier)
    out = asyncio.run(ch_api.test_channel(1, db=db, user=_u(U_OWNER, role="owner")))
    assert out["ok"] is True
    assert sent["config"]["token"] == FAKE_PK  # 内部发送用明文, 不受脱敏影响


def test_demo_can_test_own_channel(db, monkeypatch):
    class _FakeNotifier:
        def add_channel(self, type_, config):
            pass

        async def notify_with_result(self, **kwargs):
            return {"success": False, "error": "mock 失败"}

    monkeypatch.setattr(ch_api, "NotifierManager", _FakeNotifier)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(ch_api.test_channel(3, db=db, user=_u(U_DEMO)))
    assert ei.value.status_code == 500  # 归属通过后走到了真实发送(失败为 mock 预期)
