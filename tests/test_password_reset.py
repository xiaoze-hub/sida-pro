"""忘记密码(2026-10-23): 邮箱验证码重置密码链路。

覆盖:
- purpose="reset" 可发码(此前 PURPOSES 只有 register/login, 重置根本无从发起)
- 正确验证码 → 改密成功且**不自动登录**(响应无 token)
- 旧密码失效 / 新密码可登录 / 重置前签发的 token 立刻失效(token_version +1)
- 错误码、过短密码、验证码一次性、未注册邮箱
"""
import pytest
from fastapi.testclient import TestClient

from src.web.api import email_verify as ev
from src.web.api.auth import create_user
from src.web.database import SessionLocal

U = "reset-user-1023"
OLD_PW = "reset-old-pw-1023"
NEW_PW = "reset-new-pw-1023"
EMAIL = "reset-1023@example.com"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(ev, "_send_email", lambda *a, **k: True)
    ev._store.clear()

    db = SessionLocal()
    try:
        from tests.conftest import purge_users

        purge_users(db, only_username=U)
        db.commit()
        create_user(db, U, OLD_PW, "member", email=EMAIL)
    finally:
        db.close()

    from src.web.app import app

    yield TestClient(app)
    ev._store.clear()


def _issue_code(email=EMAIL, purpose="reset") -> str:
    ev.create_and_send_code(email, purpose)
    item = ev._store[f"{email}:{purpose}"]
    return item.code


def _csrf(client) -> str:
    """登录前引导 CSRF(新增 GET /api/auth/csrf), 模拟真实未登录用户流程。"""
    r = client.get("/api/auth/csrf")
    assert r.status_code == 200, r.text
    return r.json()["data"]["csrf_token"]


def _reset(client, code, password=NEW_PW, email=EMAIL):
    return client.post(
        "/api/auth/reset-password-by-email",
        json={"email": email, "code": code, "new_password": password},
        headers={"X-CSRF-Token": _csrf(client)},
    )


def _login(client, password):
    return client.post("/api/auth/login", json={"username": U, "password": password})


def test_send_code_accepts_reset_purpose(client):
    r = client.post("/api/auth/send-code", json={"email": EMAIL, "purpose": "reset"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["sent"] is True

    bad = client.post("/api/auth/send-code", json={"email": EMAIL, "purpose": "hack"})
    assert bad.status_code == 400


def test_reset_password_success_no_auto_login(client):
    old_token = _login(client, OLD_PW).json()["data"]["token"]

    code = _issue_code()
    r = _reset(client, code)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    # 不自动登录: 响应里不得有 token
    assert "token" not in r.text
    assert body["data"]["auto_login"] is False

    # 旧密码不能登录, 新密码可以
    assert _login(client, OLD_PW).status_code == 401
    ok = _login(client, NEW_PW)
    assert ok.status_code == 200, ok.text

    # 重置前签发的 token 必须失效(token_version +1)
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {old_token}"})
    assert r.status_code == 401


def test_reset_rejects_wrong_code_and_keeps_password(client):
    _issue_code()
    r = _reset(client, "000000")
    assert r.status_code == 400
    assert _login(client, OLD_PW).status_code == 200


def test_reset_rejects_short_password(client):
    code = _issue_code()
    r = _reset(client, code, password="short7")
    assert r.status_code == 400
    assert "8" in r.json()["message"]
    assert _login(client, OLD_PW).status_code == 200


def test_reset_code_is_single_use(client):
    code = _issue_code()
    assert _reset(client, code).status_code == 200
    again = _reset(client, code, password="another-pw-1023")
    assert again.status_code == 400, "验证码必须一次性"
    # 第二次重置未生效
    assert _login(client, "another-pw-1023").status_code == 401
    assert _login(client, NEW_PW).status_code == 200


def test_reset_unknown_email_after_valid_code(client):
    ghost = "ghost-1023@example.com"
    code = _issue_code(email=ghost)
    r = _reset(client, code, email=ghost)
    assert r.status_code == 404


def test_reset_rejects_bad_email_format(client):
    r = _reset(client, "123456", email="not-an-email")
    assert r.status_code == 400


def test_reset_code_purpose_isolation(client):
    """login 用途的验证码不能拿来重置密码(用途隔离, 防跨链路重放)。"""
    login_code = _issue_code(purpose="login")
    r = _reset(client, login_code)
    assert r.status_code == 400
    assert _login(client, OLD_PW).status_code == 200
