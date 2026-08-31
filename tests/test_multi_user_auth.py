"""多用户认证测试(2026-08-10 阶段1)。

覆盖: owner 自动创建/迁移、子账号 CRUD、角色权限隔离、踢人(token_version)。
"""
import pytest
from fastapi.testclient import TestClient
from src.web.database import SessionLocal
from src.web.models import User


@pytest.fixture()
def client():
    from src.web.app import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_users():
    """每个测试后清理非 admin 用户。"""
    yield
    db = SessionLocal()
    try:
        db.query(User).filter(User.username != "admin").delete()
        db.commit()
    finally:
        db.close()


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _create_member(client, token, username="alice"):
    r = client.post("/api/auth/users", headers={"Authorization": f"Bearer {token}"},
                    json={"username": username, "password": "alice12345", "role": "member"})
    assert r.status_code == 200, r.text
    return r.json()["data"]["user"]


def test_owner_auto_created(client):
    """首次访问自动创建 owner。"""
    r = client.get("/api/auth/status")
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["initialized"] is True
    assert data["user"]["role"] == "owner"
    assert data["multi_user"] is True


def test_login_and_me(client):
    """登录返回 user 信息, me 返回当前用户。"""
    token = _login(client, "admin", "xz.170530")
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["data"]["user"]["username"] == "admin"
    assert r.json()["data"]["user"]["role"] == "owner"


def test_create_member_and_login(client):
    """owner 建子账号, 子账号可登录。"""
    token = _login(client, "admin", "xz.170530")
    u = _create_member(client, token)
    assert u["role"] == "member"
    assert u["is_active"] is True

    # 子账号登录
    tok2 = _login(client, "alice", "alice12345")
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok2}"})
    assert r.json()["data"]["user"]["username"] == "alice"
    assert r.json()["data"]["user"]["role"] == "member"


def test_member_cannot_manage_users(client):
    """member 访问用户管理 → 403。"""
    token = _login(client, "admin", "xz.170530")
    _create_member(client, token)
    tok2 = _login(client, "alice", "alice12345")

    r = client.get("/api/auth/users", headers={"Authorization": f"Bearer {tok2}"})
    assert r.status_code == 403

    r = client.post("/api/auth/users", headers={"Authorization": f"Bearer {tok2}"},
                    json={"username": "bob", "password": "bob123456", "role": "member"})
    assert r.status_code == 403


def test_duplicate_username_rejected(client):
    """重复用户名 → 400。"""
    token = _login(client, "admin", "xz.170530")
    r = client.post("/api/auth/users", headers={"Authorization": f"Bearer {token}"},
                    json={"username": "admin", "password": "xz.17053045", "role": "member"})
    assert r.status_code == 400


def test_disable_user_kicks_token(client):
    """禁用用户后其 token 失效。"""
    token = _login(client, "admin", "xz.170530")
    u = _create_member(client, token)
    tok2 = _login(client, "alice", "alice12345")

    # 禁用 alice
    r = client.patch(f"/api/auth/users/{u['id']}", headers={"Authorization": f"Bearer {token}"},
                     json={"is_active": False})
    assert r.status_code == 200

    # alice 旧 token 失效
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok2}"})
    assert r.status_code in (401, 403)


def test_change_password_bumps_token(client):
    """改密后旧 token 失效(踢人)。"""
    token = _login(client, "admin", "xz.170530")
    u = _create_member(client, token)
    tok2 = _login(client, "alice", "alice12345")

    # owner 给 alice 改密
    r = client.patch(f"/api/auth/users/{u['id']}", headers={"Authorization": f"Bearer {token}"},
                     json={"password": "newpass123"})
    assert r.status_code == 200

    # alice 旧 token 失效
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok2}"})
    assert r.status_code == 401

    # 新密码可登录
    _login(client, "alice", "newpass123")


def test_cannot_disable_self(client):
    """owner 不能禁用自己。"""
    token = _login(client, "admin", "xz.170530")
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["data"]["user"]
    r = client.patch(f"/api/auth/users/{me['id']}", headers={"Authorization": f"Bearer {token}"},
                     json={"is_active": False})
    assert r.status_code == 400
