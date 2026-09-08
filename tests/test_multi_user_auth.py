"""多用户认证测试(2026-08-10 阶段1)。

覆盖: owner 自动创建/迁移、子账号 CRUD、角色权限隔离、踢人(token_version)。
"""
import pytest
from fastapi.testclient import TestClient
from src.web.database import SessionLocal
from src.web.models import User

# 0.6 删除固定默认密码后, 测试用 AUTH_USERNAME/AUTH_PASSWORD
# 环境变量确定性引导 owner(见 client fixture)。
ADMIN_USER = "admin"
ADMIN_PW = "admin-test-0606"


@pytest.fixture()
def client(monkeypatch):
    # 登录限流(敏感路径 20/min/IP)在测试进程内跨文件累计 → 合跑 429, 单测关闭
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    monkeypatch.setenv("AUTH_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AUTH_PASSWORD", ADMIN_PW)
    # 清掉库中已有 admin, 强制本次走 env 引导路径(密码确定)
    db = SessionLocal()
    try:
        db.query(User).filter(User.username == ADMIN_USER).delete()
        db.commit()
    finally:
        db.close()
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
    token = _login(client, ADMIN_USER, ADMIN_PW)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["data"]["user"]["username"] == "admin"
    assert r.json()["data"]["user"]["role"] == "owner"


def test_create_member_and_login(client):
    """owner 建子账号, 子账号可登录。"""
    token = _login(client, ADMIN_USER, ADMIN_PW)
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
    token = _login(client, ADMIN_USER, ADMIN_PW)
    _create_member(client, token)
    tok2 = _login(client, "alice", "alice12345")

    r = client.get("/api/auth/users", headers={"Authorization": f"Bearer {tok2}"})
    assert r.status_code == 403

    r = client.post("/api/auth/users", headers={"Authorization": f"Bearer {tok2}"},
                    json={"username": "bob", "password": "bob123456", "role": "member"})
    assert r.status_code == 403


def test_duplicate_username_rejected(client):
    """重复用户名 → 400。"""
    token = _login(client, ADMIN_USER, ADMIN_PW)
    r = client.post("/api/auth/users", headers={"Authorization": f"Bearer {token}"},
                    json={"username": "admin", "password": "member-Test-45", "role": "member"})
    assert r.status_code == 400


def test_disable_user_kicks_token(client):
    """禁用用户后其 token 失效。"""
    token = _login(client, ADMIN_USER, ADMIN_PW)
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
    token = _login(client, ADMIN_USER, ADMIN_PW)
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
    token = _login(client, ADMIN_USER, ADMIN_PW)
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).json()["data"]["user"]
    r = client.patch(f"/api/auth/users/{me['id']}", headers={"Authorization": f"Bearer {token}"},
                     json={"is_active": False})
    assert r.status_code == 400
