"""后台用户控制台回归(2026-10-23): 角色↔Key 档位联动 / 注册模式 / 恢复 / 建号重置。

钉住的事实:
- (c) change-role→pro 后 active key 变 pro 档; 降级回 free 档并写 TierDowngradeLog
- (d) PUT register-mode 后 get_register_mode 立刻反映(两条等价路径)
- (e) restore 后 is_deleted=False 且 is_active=True
- 建号/重置密码/列表字段(邮箱/注册时间/最后登录/注销态)齐全
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.invite_codes import get_register_mode
from src.db.models import AppSettings, SkillApiKey, TierDowngradeLog, User
from src.web.api import auth, pro_billing, users as users_mod
from src.web.database import Base, get_db
from src.web.api.skills_gateway import TIER_DAILY_LIMIT


@pytest.fixture()
def env(monkeypatch):
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
        id=str(uuid.uuid4()),
        username="bob",
        email="bob@example.com",
        password_hash="x",
        role="member",
        is_active=True,
    )
    db.add_all([owner, member])
    db.commit()
    owner_id, member_id = owner.id, member.id
    db.close()

    app = FastAPI()
    app.include_router(users_mod.router, prefix="/api/users")
    app.include_router(pro_billing.router, prefix="/api")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    def _require_owner():
        s = Session()
        try:
            return s.query(User).filter(User.id == owner_id).first()
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[auth.require_owner] = _require_owner

    audits: list[tuple[str, str]] = []
    monkeypatch.setattr(
        users_mod, "log_audit", lambda db_, user_, action, detail="": audits.append((action, detail))
    )

    return {
        "client": TestClient(app),
        "Session": Session,
        "owner_id": owner_id,
        "member_id": member_id,
        "audits": audits,
    }


def test_change_role_to_pro_upgrades_keys_then_downgrades(env):
    """(c) 手工改角色也要同步 Key 档位(此前只改 users.role)。"""
    s = env["Session"]()
    try:
        s.add(SkillApiKey(
            key_hash=str(uuid.uuid4()), key_prefix="k1", user_id=env["member_id"],
            tier="free", status="active", daily_limit=TIER_DAILY_LIMIT["free"],
        ))
        s.commit()
    finally:
        s.close()

    c = env["client"]
    r = c.post(f"/api/users/admin/{env['member_id']}/change-role", json={"role": "pro"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "pro"
    assert r.json()["keys_changed"] == 1

    s = env["Session"]()
    try:
        k = s.query(SkillApiKey).filter(SkillApiKey.user_id == env["member_id"]).first()
        assert k.tier == "pro"
        assert k.daily_limit == TIER_DAILY_LIMIT["pro"]
    finally:
        s.close()

    r = c.post(f"/api/users/admin/{env['member_id']}/change-role", json={"role": "member"})
    assert r.status_code == 200, r.text
    assert r.json()["keys_changed"] == 1

    s = env["Session"]()
    try:
        k = s.query(SkillApiKey).filter(SkillApiKey.user_id == env["member_id"]).first()
        assert k.tier == "free"
        assert k.daily_limit == TIER_DAILY_LIMIT["free"]
        log = s.query(TierDowngradeLog).filter(TierDowngradeLog.user_id == env["member_id"]).all()
        assert len(log) == 1
        assert log[0].from_tier == "pro" and log[0].to_tier == "free"
    finally:
        s.close()

    # 管理端写操作必须落审计
    assert any(a[0] == "manage_user" and "change_role" in a[1] for a in env["audits"])


def test_role_choices_include_guest(env):
    """四档角色统一: guest 也能被设置(旧枚举缺 guest)。"""
    c = env["client"]
    r = c.post(f"/api/users/admin/{env['member_id']}/change-role", json={"role": "guest"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "guest"

    bad = c.post(f"/api/users/admin/{env['member_id']}/change-role", json={"role": "admin"})
    assert bad.status_code == 400


def test_register_mode_put_takes_effect_immediately(env):
    """(d) PUT /api/admin/register-mode 后 get_register_mode 立刻读到新值。"""
    c = env["client"]
    assert c.put("/api/admin/register-mode", json={"mode": "open"}).status_code == 200

    s = env["Session"]()
    try:
        assert get_register_mode(s) == "open"
        assert s.query(AppSettings).filter(AppSettings.key == "register_mode").count() == 1
    finally:
        s.close()

    # 等价路径(/api/users/admin/register-mode)写同一份设置
    assert c.put("/api/users/admin/register-mode", json={"mode": "closed"}).status_code == 200
    s = env["Session"]()
    try:
        assert get_register_mode(s) == "closed"
        assert s.query(AppSettings).filter(AppSettings.key == "register_mode").count() == 1
    finally:
        s.close()

    assert c.put("/api/admin/register-mode", json={"mode": "whatever"}).status_code == 400
    assert any(a[0] == "manage_user" and "register_mode" in a[1] for a in env["audits"])


def test_restore_soft_deleted_user(env):
    """(e) 宽限期内的注销账号可恢复: is_deleted=False 且 is_active=True。"""
    s = env["Session"]()
    try:
        u = s.query(User).filter(User.id == env["member_id"]).first()
        u.is_deleted = True
        u.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=3)
        u.is_active = False
        s.commit()
    finally:
        s.close()

    c = env["client"]
    rows = c.get("/api/users/admin/list").json()["users"]
    row = next(r for r in rows if r["username"] == "bob")
    assert row["is_deleted"] is True
    assert row["deleted_at"] is not None

    r = c.post(f"/api/users/admin/{env['member_id']}/restore")
    assert r.status_code == 200, r.text
    assert r.json()["is_deleted"] is False
    assert r.json()["is_active"] is True

    s = env["Session"]()
    try:
        u = s.query(User).filter(User.id == env["member_id"]).first()
        assert u.is_deleted is False
        assert u.deleted_at is None
        assert u.is_active is True
    finally:
        s.close()

    # 未注销的账号再恢复 → 400
    assert c.post(f"/api/users/admin/{env['member_id']}/restore").status_code == 400


def test_admin_create_user_and_reset_password(env):
    """后台建号 + 重置密码(此前只能看不能管)。"""
    c = env["client"]
    r = c.post("/api/users/admin/create", json={
        "username": "carol", "password": "passw0rd!", "role": "pro", "email": "carol@example.com",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "carol" and body["role"] == "pro"
    assert body["email"] == "carol@example.com"

    assert c.post("/api/users/admin/create", json={
        "username": "carol", "password": "passw0rd!", "role": "member",
    }).status_code == 400
    assert c.post("/api/users/admin/create", json={
        "username": "dave", "password": "short", "role": "member",
    }).status_code == 400
    assert c.post("/api/users/admin/create", json={
        "username": "dave", "password": "passw0rd!", "role": "admin",
    }).status_code == 400

    uid = body["id"]
    rp = c.post(f"/api/users/admin/{uid}/reset-password", json={"new_password": "newpass123"})
    assert rp.status_code == 200, rp.text

    from src.web.api.auth import verify_password

    s = env["Session"]()
    try:
        u = s.query(User).filter(User.id == uid).first()
        assert verify_password("newpass123", u.password_hash)
        assert u.token_version == 2  # 重置后踢掉旧 token
    finally:
        s.close()


def test_admin_list_exposes_email_created_last_login(env):
    """列表字段补齐: 邮箱/注册时间/最后登录(设置页与后台共用同一份数据)。"""
    rows = env["client"].get("/api/users/admin/list").json()["users"]
    bob = next(r for r in rows if r["username"] == "bob")
    for key in ("email", "created_at", "last_login", "is_deleted", "deleted_at", "is_active"):
        assert key in bob
    assert bob["email"] == "bob@example.com"
    assert bob["created_at"]
