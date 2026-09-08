"""0.6 自助改密回归: change-password 旧密码校验 + token_version 踢人(2026-09-08)。

owner 删除固定默认密码后, 首登改密是唯一入口, 本文件保证改密链路行为正确:
- 旧密码错误 → 400, 密码不变
- 改密成功 → 旧 token 失效、旧密码不能再登录、新密码可登录
- 新密码 < 8 位 → 400
"""
import pytest
from fastapi.testclient import TestClient
from src.web.database import SessionLocal
from src.web.models import User

ADMIN_USER = "admin"
ADMIN_PW = "chg-pw-old-0606"
NEW_PW = "chg-pw-new-0606"


@pytest.fixture()
def client(monkeypatch):
    # 登录/改密限流(敏感路径 20/min/IP)在测试进程内跨文件累计 → 合跑 429, 单测关闭
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


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _change(client, token, old, new):
    return client.post(
        "/api/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"old_password": old, "new_password": new},
    )


def test_change_password_wrong_old_rejected(client):
    """旧密码错误 → 400, 且原密码仍可登录(密码未被动过)。"""
    token = _login(client, ADMIN_USER, ADMIN_PW)
    r = _change(client, token, "totally-wrong-old", NEW_PW)
    assert r.status_code == 400, r.text
    assert _login(client, ADMIN_USER, ADMIN_PW)


def test_change_password_too_short_rejected(client):
    """新密码 < 8 位 → 400。"""
    token = _login(client, ADMIN_USER, ADMIN_PW)
    r = _change(client, token, ADMIN_PW, "short7")
    assert r.status_code == 400, r.text
    assert _login(client, ADMIN_USER, ADMIN_PW)


def test_change_password_ok_kicks_old_token(client):
    """改密成功: 旧 token 失效(token_version +1), 旧密码不能再登录, 新密码可登录。"""
    token = _login(client, ADMIN_USER, ADMIN_PW)
    r = _change(client, token, ADMIN_PW, NEW_PW)
    assert r.status_code == 200, r.text

    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (401, 403), "改密后旧 token 必须失效"

    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PW})
    assert r.status_code in (400, 401), "旧密码必须不能再登录"

    assert _login(client, ADMIN_USER, NEW_PW)
