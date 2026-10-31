"""验证码接入登录接口：出现时机、错误语义、机器豁免、锁定仍生效。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.core import captcha as cap
from src.core import login_ratelimit
from src.web.api import captcha_store

ADMIN_USER = "admin"
ADMIN_PW = "cap-pw-2026-1031"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    monkeypatch.setenv("AUTH_CAPTCHA_SECRET", "unit-test-secret-0123456789")
    monkeypatch.setenv("AUTH_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AUTH_PASSWORD", ADMIN_PW)
    login_ratelimit.reset()
    db = __import__("src.web.database", fromlist=["SessionLocal"]).SessionLocal()
    try:
        from tests.conftest import purge_users

        purge_users(db, only_username=ADMIN_USER)
        db.commit()
    finally:
        db.close()
    from src.web.app import app

    yield TestClient(app)
    login_ratelimit.reset()


def _issue(client, monkeypatch) -> tuple[str, str]:
    """取一张验证码，并把答案截下来（浏览器里由人看，测试里只能自己记）。"""
    seen: dict[str, str] = {}
    real = cap.new_captcha

    def spy(**kwargs):
        item = real(**kwargs)
        seen["answer"] = item.answer
        return item

    monkeypatch.setattr(cap, "new_captcha", spy)
    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "always")  # conftest 默认 off，这里显式打开
    r = client.get("/api/auth/captcha")
    assert r.status_code == 200, r.text
    assert r.headers.get("cache-control") == "no-store"
    body = r.json()["data"]  # 统一响应包装 {code,success,data,message}
    assert body["enabled"] is True and body["captcha_id"] and seen.get("answer")
    assert body["image"].startswith("data:image/png;base64,")
    return body["captcha_id"], seen["answer"]


def test_answer_never_appears_in_the_issued_token(client, monkeypatch):
    captcha_id, answer = _issue(client, monkeypatch)
    assert answer.lower() not in captcha_id.lower()


def test_mode_off_reports_disabled_with_no_store(client, monkeypatch):
    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "off")
    r = client.get("/api/auth/captcha")
    body = r.json()["data"]
    assert body["enabled"] is False and body["image"] is None
    assert r.headers.get("cache-control") == "no-store", "off 分支也必须不缓存"


def test_always_mode_requires_captcha_before_password(client, monkeypatch):
    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "always")
    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PW})
    assert r.status_code == 428, r.text
    assert "验证码" in r.json()["message"]


def test_wrong_captcha_does_not_lock_the_account(client, monkeypatch):
    captcha_id, answer = _issue(client, monkeypatch)
    wrong = "ZZZZZ" if answer != "ZZZZZ" else "YYYYY"
    r = client.post("/api/auth/login", json={
        "username": ADMIN_USER, "password": ADMIN_PW,
        "captcha_id": captcha_id, "captcha_code": wrong,
    })
    assert r.status_code == 428
    assert login_ratelimit.failures("testclient", ADMIN_USER) == 0


def test_correct_captcha_lets_login_through(client, monkeypatch):
    captcha_id, answer = _issue(client, monkeypatch)
    r = client.post("/api/auth/login", json={
        "username": ADMIN_USER, "password": ADMIN_PW,
        "captcha_id": captcha_id, "captcha_code": answer.lower(),
    })
    assert r.status_code == 200, r.text
    assert r.json()["data"]["token"]


def test_captcha_is_single_use_across_requests(client, monkeypatch):
    captcha_id, answer = _issue(client, monkeypatch)
    payload = {"username": ADMIN_USER, "password": ADMIN_PW,
               "captcha_id": captcha_id, "captcha_code": answer}
    assert client.post("/api/auth/login", json=payload).status_code == 200
    assert client.post("/api/auth/login", json=payload).status_code == 428


def test_service_token_header_skips_captcha(client, monkeypatch):
    """机器身份豁免：带对的 X-Service-Token 直接登录，不要求验证码。"""
    import src.web.api.auth as auth_mod

    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "always")
    monkeypatch.setattr(auth_mod, "_service_token", "svc-token-xyz", raising=False)
    r = client.post(
        "/api/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PW},
        headers={"X-Service-Token": "svc-token-xyz"},
    )
    assert r.status_code == 200, r.text


def test_wrong_service_token_still_needs_captcha(client, monkeypatch):
    import src.web.api.auth as auth_mod

    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "always")
    monkeypatch.setattr(auth_mod, "_service_token", "svc-token-xyz", raising=False)
    r = client.post(
        "/api/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PW},
        headers={"X-Service-Token": "not-it"},
    )
    assert r.status_code == 428, r.text


def test_lockout_still_fires_when_captcha_is_solved(client, monkeypatch):
    """验证码不是替代锁定：口令错 5 次照样锁（每次都得先过验证码）。"""
    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "always")
    codes = [_issue(client, monkeypatch) for _ in range(login_ratelimit.MAX_FAILS)]
    for captcha_id, answer in codes:
        r = client.post("/api/auth/login", json={
            "username": ADMIN_USER, "password": "definitely-wrong",
            "captcha_id": captcha_id, "captcha_code": answer,
        })
        assert r.status_code == 401, r.text
    locked = client.post("/api/auth/login", json={
        "username": ADMIN_USER, "password": ADMIN_PW,
    })
    assert locked.status_code == 429, locked.text


def test_adaptive_mode_allows_credential_login_without_captcha(client, monkeypatch):
    """自适应模式下零失败的正确凭据不会被拦（给没有服务令牌的老部署留退路）。"""
    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "adaptive")
    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PW})
    assert r.status_code == 200, r.text


def test_expired_token_is_rejected_even_if_row_survives(client, monkeypatch):
    """token 过期即拒（不必等存储层）。"""
    monkeypatch.setenv("AUTH_CAPTCHA_MODE", "always")
    item = cap.new_captcha(now=1000)
    r = client.post("/api/auth/login", json={
        "username": ADMIN_USER, "password": ADMIN_PW,
        "captcha_id": item.captcha_id, "captcha_code": item.answer,
    })
    assert r.status_code == 428
