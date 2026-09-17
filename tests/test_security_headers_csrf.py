"""CSRF + 安全响应头 单元测试 (tier2-stability 2026-09-18)

覆盖:
- SecurityHeadersMiddleware: CSP / X-Frame-Options 等头存在
- CSRFProtectionMiddleware:
  - GET 跳过
  - POST /api/auth/login 豁免
  - POST /api/webhooks/* 豁免
  - Bearer JWT 跳过(与现前端兼容)
  - 无 Cookie/Header → 403
  - Cookie 与 Header 不一致 → 403
  - Cookie 与 Header 一致 → 放行
- issue_csrf_token: Cookie 属性 HttpOnly + SameSite=Strict
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from src.web.middleware import (
    CSP_POLICY,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    CSRFProtectionMiddleware,
    SecurityHeadersMiddleware,
    issue_csrf_token,
)


async def _ok(request):
    return JSONResponse({"ok": True})


def _build_app(with_csrf: bool = True) -> Starlette:
    app = Starlette(routes=[Route("/api/test", _ok, methods=["GET", "POST", "PUT", "DELETE", "PATCH"]),
                            Route("/api/auth/login", _ok, methods=["POST"]),
                            Route("/api/auth/register", _ok, methods=["POST"]),
                            Route("/api/webhooks/tv", _ok, methods=["POST"])])
    if with_csrf:
        app.add_middleware(CSRFProtectionMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def test_security_headers_present():
    client = TestClient(_build_app(with_csrf=False))
    r = client.get("/api/test")
    assert r.status_code == 200
    assert r.headers.get("Content-Security-Policy") == CSP_POLICY
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-XSS-Protection") == "1; mode=block"
    assert r.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
    assert "camera=()" in r.headers.get("Permissions-Policy", "")
    assert "default-src 'self'" in CSP_POLICY
    assert "frame-ancestors 'none'" in CSP_POLICY


def test_csrf_skips_safe_methods():
    client = TestClient(_build_app())
    assert client.get("/api/test").status_code == 200


def test_csrf_skips_login_and_webhooks():
    client = TestClient(_build_app())
    assert client.post("/api/auth/login", json={}).status_code == 200
    assert client.post("/api/auth/register", json={}).status_code == 200
    assert client.post("/api/webhooks/tv", json={}).status_code == 200


def test_csrf_skips_bearer_jwt():
    client = TestClient(_build_app())
    r = client.post("/api/test", json={}, headers={"Authorization": "Bearer abc.def.ghi"})
    assert r.status_code == 200


def test_csrf_skips_service_token():
    client = TestClient(_build_app())
    r = client.post("/api/test", json={}, headers={"X-Service-Token": "svc-secret"})
    assert r.status_code == 200


def test_csrf_missing_token_rejected():
    client = TestClient(_build_app())
    r = client.post("/api/test", json={})
    assert r.status_code == 403
    body = r.json()
    assert body.get("code") == 403


def test_csrf_mismatch_rejected():
    client = TestClient(_build_app())
    client.cookies.set(CSRF_COOKIE_NAME, "aaa")
    r = client.post("/api/test", json={}, headers={CSRF_HEADER_NAME: "bbb"})
    assert r.status_code == 403


def test_csrf_match_allowed():
    client = TestClient(_build_app())
    client.cookies.set(CSRF_COOKIE_NAME, "tok-123")
    r = client.post("/api/test", json={}, headers={CSRF_HEADER_NAME: "tok-123"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_issue_csrf_token_sets_httponly_strict_cookie():
    from starlette.responses import Response

    resp = Response()
    token = issue_csrf_token(resp)
    assert len(token) == 64  # 32 bytes hex
    set_cookie = resp.headers["set-cookie"].lower()
    assert "csrf_token=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/" in set_cookie
