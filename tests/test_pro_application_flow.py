"""Pro 申请→审核闭环回归(2026-10-23)。

钉住的核心事实:
- `POST /api/profile/pro-apply`(个人中心)与 `POST /api/pro/apply`(档位页)
  **都**落 `pro_applications` 表、都给 owner 发站内通知、都拒重复 pending。
  历史 bug: profile 入口只写审计+通知不落库 → 后台审核列表永远为空。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import Notification, ProApplication, SkillApiKey, User
from src.web.api import auth, pro_billing, profile
from src.web.database import Base, get_db


@pytest.fixture()
def env():
    """内存库 + 独立 app(只挂 pro_billing / profile 路由) + 可切换当前用户。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    db = Session()
    owner = User(
        id=str(uuid.uuid4()), username="boss", password_hash="x", role="owner", is_active=True
    )
    member = User(
        id=str(uuid.uuid4()), username="alice", password_hash="x", role="member", is_active=True
    )
    db.add_all([owner, member])
    db.commit()
    owner_id, member_id = owner.id, member.id
    db.close()

    app = FastAPI()
    app.include_router(pro_billing.router, prefix="/api")
    app.include_router(profile.router, prefix="/api/profile")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db

    state = {"uid": member_id}

    def _current_user():
        s = Session()
        try:
            return s.query(User).filter(User.id == state["uid"]).first()
        finally:
            s.close()

    def _require_owner():
        s = Session()
        try:
            return s.query(User).filter(User.id == owner_id).first()
        finally:
            s.close()

    app.dependency_overrides[auth.get_current_user] = _current_user
    app.dependency_overrides[auth.require_owner] = _require_owner

    return {
        "client": TestClient(app),
        "Session": Session,
        "state": state,
        "owner_id": owner_id,
        "member_id": member_id,
    }


def _pending(client: TestClient) -> list[dict]:
    r = client.get("/api/pro/admin/applications", params={"status": "pending"})
    assert r.status_code == 200, r.text
    return r.json()["applications"]


def test_profile_pro_apply_persists_and_visible_to_admin(env):
    """(a) 个人中心入口申请后, 后台 pending 列表能查到该申请。"""
    c = env["client"]
    r = c.post("/api/profile/pro-apply", json={"reason": "想看主力意图"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    assert body["application"]["id"]

    apps = _pending(c)
    assert [a["username"] for a in apps] == ["alice"]
    assert apps[0]["reason"] == "想看主力意图"

    s = env["Session"]()
    try:
        assert s.query(ProApplication).filter(ProApplication.user_id == env["member_id"]).count() == 1
    finally:
        s.close()


def test_profile_pro_apply_notifies_owner(env):
    """个人中心入口也要通知 owner(原实现通知了但没落库, 现在两者都要有)。"""
    c = env["client"]
    assert c.post("/api/profile/pro-apply", json={"reason": "升级"}).status_code == 200

    s = env["Session"]()
    try:
        rows = s.query(Notification).filter(Notification.user_id == env["owner_id"]).all()
        assert len(rows) == 1
        assert rows[0].title == "Pro 升级申请"
        assert "alice" in rows[0].body
    finally:
        s.close()


def test_duplicate_pending_rejected_on_both_entries(env):
    """(b) 同用户重复申请被拒: profile 入口写过之后, /api/pro/apply 也拒; 反之亦然。"""
    c = env["client"]
    assert c.post("/api/profile/pro-apply", json={"reason": "第一次"}).status_code == 200

    r2 = c.post("/api/profile/pro-apply", json={"reason": "第二次"})
    assert r2.status_code == 400, r2.text
    assert "待审核" in r2.json()["detail"]

    r3 = c.post("/api/pro/apply", json={"reason": "换个入口"})
    assert r3.status_code == 400, r3.text
    assert "待审核" in r3.json()["detail"]

    assert len(_pending(c)) == 1


def test_pro_apply_public_entry_persists_and_notifies(env):
    """/api/pro/apply 与 profile 入口等价: 落库 + 通知 owner, 也能被后台审核。"""
    c = env["client"]
    r = c.post("/api/pro/apply", json={"reason": "档位页申请", "contact": "wx-123"})
    assert r.status_code == 200, r.text
    assert "[contact] wx-123" in r.json()["application"]["reason"]

    apps = _pending(c)
    assert len(apps) == 1 and apps[0]["username"] == "alice"

    s = env["Session"]()
    try:
        assert s.query(Notification).filter(Notification.user_id == env["owner_id"]).count() == 1
    finally:
        s.close()


def test_already_pro_rejected(env):
    """已是 pro 的账号申请 → 400(两入口同一口径)。"""
    s = env["Session"]()
    try:
        u = s.query(User).filter(User.id == env["member_id"]).first()
        u.role = "pro"
        s.commit()
    finally:
        s.close()

    c = env["client"]
    assert c.post("/api/profile/pro-apply", json={"reason": "x"}).status_code == 400
    assert c.post("/api/pro/apply", json={"reason": "x"}).status_code == 400


def test_approve_then_reapply_flow(env):
    """审核闭环: 批准 → role=pro + key 升档; 列表里该申请状态变 approved。"""
    c = env["client"]
    s = env["Session"]()
    try:
        key = SkillApiKey(key_hash=str(uuid.uuid4()), key_prefix="abc", user_id=env["member_id"])
        s.add(key)
        s.commit()
    finally:
        s.close()

    assert c.post("/api/profile/pro-apply", json={"reason": "申请"}).status_code == 200
    app_id = _pending(c)[0]["id"]

    r = c.post("/api/pro/admin/approve", json={"application_id": app_id, "note": "通过"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["user"]["role"] == "pro"
    assert r.json()["keys_upgraded"] == 1

    s = env["Session"]()
    try:
        k = s.query(SkillApiKey).filter(SkillApiKey.user_id == env["member_id"]).first()
        assert k.tier == "pro"
        assert s.query(ProApplication).filter(ProApplication.id == app_id).first().status == "approved"
    finally:
        s.close()

    # 已批准: 再申请可以(不再有 pending), 后台 pending 为空
    assert c.post("/api/profile/pro-apply", json={"reason": "再来"}).status_code == 400  # 已是 pro
    assert _pending(c) == []
