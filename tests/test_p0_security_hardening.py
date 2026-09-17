"""P0 安全加固测试 (2026-09-18)

覆盖:
- HSTS: HTTPS 请求带 Strict-Transport-Security; HTTP 不带
- JWT httpOnly Cookie: login 下发 Cookie 属性正确
- get_current_user: Cookie 优先 / Bearer fallback
- 密钥轮换: 旧 secret grace period 内仍可验签; 过期后不可
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.web.middleware import (
    HSTS_VALUE,
    SecurityHeadersMiddleware,
    _request_is_https,
)


async def _ok(request):
    return JSONResponse({"ok": True})


def _build_app() -> Starlette:
    app = Starlette(routes=[Route("/api/test", _ok, methods=["GET", "POST"])])
    app.add_middleware(SecurityHeadersMiddleware)
    return app


# ─── HSTS ─────────────────────────────────────────────────────────────


def test_hsts_absent_on_http():
    client = TestClient(_build_app())
    r = client.get("/api/test")
    assert r.status_code == 200
    assert "strict-transport-security" not in {k.lower() for k in r.headers}


def test_hsts_present_on_https_via_forwarded_proto():
    client = TestClient(_build_app())
    r = client.get("/api/test", headers={"X-Forwarded-Proto": "https"})
    assert r.status_code == 200
    assert r.headers.get("Strict-Transport-Security") == HSTS_VALUE
    assert "max-age=31536000" in HSTS_VALUE
    assert "includeSubDomains" in HSTS_VALUE


def test_request_is_https_helper():
    from starlette.requests import Request

    scope_https = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"x-forwarded-proto", b"https")],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
    }
    assert _request_is_https(Request(scope_https)) is True

    scope_http = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
    }
    assert _request_is_https(Request(scope_http)) is False


# ─── Cookie 属性 ──────────────────────────────────────────────────────


def test_set_auth_cookie_attributes():
    from starlette.requests import Request
    from starlette.responses import Response

    from src.web.api.auth import AUTH_COOKIE_NAME, set_auth_cookie

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [(b"x-forwarded-proto", b"https")],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
    }
    request = Request(scope)
    response = Response()
    set_auth_cookie(response, request, "test.jwt.value")

    raw = response.headers.get("set-cookie", "")
    assert AUTH_COOKIE_NAME in raw
    assert "HttpOnly" in raw
    assert "SameSite=strict" in raw.lower() or "samesite=strict" in raw.lower()
    assert "Path=/" in raw
    assert "Secure" in raw  # https via X-Forwarded-Proto
    assert "Max-Age=43200" in raw  # 12h


def test_set_auth_cookie_no_secure_on_http():
    from starlette.requests import Request
    from starlette.responses import Response

    from src.web.api.auth import set_auth_cookie

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
    }
    response = Response()
    set_auth_cookie(response, Request(scope), "tok")
    raw = response.headers.get("set-cookie", "")
    assert "Secure" not in raw


# ─── extract_token_from_request ───────────────────────────────────────


def test_extract_token_bearer_wins_over_cookie():
    """**Bearer 优先于 Cookie**(2026-09-18 老板拍板改口径; 原为 Cookie 优先)。

    原口径的副作用是身份静默错位: 浏览器残留上一个账号的 Cookie 时, 显式带了 Bearer 的请求
    会被按另一个用户执行。新口径: 有 Authorization header ⇒ 它就是权威身份
    (验不过直接 401, 不回退 Cookie —— 见 tests/test_auth_bearer_priority.py)。
    """
    from starlette.requests import Request

    from src.web.api.auth import AUTH_COOKIE_NAME, extract_token_from_request, token_from_request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/auth/me",
        "headers": [
            (b"cookie", f"{AUTH_COOKIE_NAME}=cookie-token".encode()),
            (b"authorization", b"Bearer header-token"),
        ],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
    }
    request = Request(scope)
    # Starlette Request.cookies 从 header 解析
    assert request.cookies.get(AUTH_COOKIE_NAME) == "cookie-token"
    assert extract_token_from_request(request) == "header-token"
    # 中间件/审计侧同源裁决函数必须给出同一答案(单一真源)
    assert token_from_request(request) == "header-token"


def test_extract_token_fallback_bearer():
    from starlette.requests import Request

    from src.web.api.auth import extract_token_from_request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/auth/me",
        "headers": [(b"authorization", b"Bearer header-token")],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
    }
    assert extract_token_from_request(Request(scope)) == "header-token"


# ─── 多 secret 验签(grace period) ────────────────────────────────────


def test_decode_token_tries_grace_secrets(monkeypatch):
    """旧 secret 签的 token 在 grace 期内仍可 decode。"""
    import jwt as pyjwt

    from src.core import auth_tokens

    current = "current-secret-current-secret-32b!!"
    old = "old-secret-old-secret-old-secret-32b!!!"

    monkeypatch.setattr(auth_tokens, "get_jwt_secret", lambda: current)
    monkeypatch.setattr(
        auth_tokens,
        "get_verification_secrets",
        lambda: [current, old],
    )

    token_old = pyjwt.encode(
        {"sub": "u1", "exp": 9999999999, "ver": 0},
        old,
        algorithm="HS256",
    )
    token_new = pyjwt.encode(
        {"sub": "u2", "exp": 9999999999, "ver": 0},
        current,
        algorithm="HS256",
    )
    token_bad = pyjwt.encode(
        {"sub": "u3", "exp": 9999999999, "ver": 0},
        "neither-secret-neither-secret-32b",
        algorithm="HS256",
    )

    assert auth_tokens.decode_token(token_old)["sub"] == "u1"
    assert auth_tokens.decode_token(token_new)["sub"] == "u2"
    assert auth_tokens.decode_token(token_bad) is None
