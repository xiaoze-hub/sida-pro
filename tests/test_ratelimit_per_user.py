"""每用户限流回归(2026-09-07 0.8): 限流分桶曾取不存在的 user_id claim。

JWT payload 的用户 id 在 sub 字段(auth.create_token), 原 JWTDecodeMiddleware
写 payload.get("user_id") 恒为 None → 限流分桶永远走 IP → 同出口 IP 的
4 账号(多用户并存)一人跑重活把其他人一起限流。

覆盖:
  - principal_from_payload: sub 优先 / 兜底历史 user_id / 空值安全
  - JWTDecodeMiddleware: 真 JWT(create_token→decode_token 闭环)下
    request.state.user["user_id"] 等于 sub(0.8 核心断言)
  - RateLimitMiddleware: 用户 A 打满 429, 同 IP 用户 B 不受影响; 匿名按 IP
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.web.api import auth as auth_mod
from src.web.cache.redis_client import redis_client as _redis_singleton
from src.web.middleware import JWTDecodeMiddleware, RateLimitMiddleware, _in_memory_bucket
from src.web.models import User


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret-0808-wave0-ratelimit-regression")
    monkeypatch.setattr(auth_mod, "_jwt_secret", None)  # 强制重读本次 env
    monkeypatch.setattr(_redis_singleton, "_enabled", False)  # 强制走内存桶, 不依赖外部 Redis
    monkeypatch.setattr(_redis_singleton, "_client", None)
    _in_memory_bucket._buckets.clear()

    app = FastAPI()

    @app.get("/api/ping")
    def ping(request: Request):
        return {"user": getattr(request.state, "user", None)}

    # 后 add 的在外层: JWTDecode 先执行, RateLimit 才能读到 state.user
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(JWTDecodeMiddleware)
    with TestClient(app) as c:
        yield c


def _make_token(user_id: str, username: str) -> str:
    user = User(id=user_id, username=username, role="member", token_version=1)
    token, _ = auth_mod.create_token(user)
    return token


def test_principal_from_payload_prefers_sub():
    """payload 的用户 id 应优先取 sub 字段(create_token 的真实形状)。"""
    assert auth_mod.principal_from_payload(
        {"sub": "u1", "username": "alice", "role": "member"}
    ) == {"user_id": "u1", "username": "alice", "role": "member"}


def test_principal_from_payload_falls_back_to_legacy_user_id():
    """历史兼容: 旧 payload 只有 user_id claim 时仍可用。"""
    assert auth_mod.principal_from_payload({"user_id": "u2"})["user_id"] == "u2"


def test_principal_from_payload_empty():
    """空 payload 返回空 dict, 不抛异常。"""
    assert auth_mod.principal_from_payload(None) == {}


def test_jwt_decode_middleware_sets_user_id(client):
    """真 JWT 闭环: state.user.user_id 应等于 token 的 sub(修复前恒为 None)。"""
    token = _make_token("u-abc", "alice")
    r = client.get("/api/ping", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["user"]["user_id"] == "u-abc"


def test_ratelimit_per_user_not_shared(client, monkeypatch):
    """用户 A 打满 429 后, 同出口 IP 的用户 B 不受影响(修复前两人同桶 B 也 429)。"""
    monkeypatch.setenv("RATE_LIMIT_GET", "3")
    tok_a = _make_token("u-a", "alice")
    tok_b = _make_token("u-b", "bob")
    codes_a = [
        client.get("/api/ping", headers={"Authorization": f"Bearer {tok_a}"}).status_code
        for _ in range(4)
    ]
    assert codes_a[:3] == [200, 200, 200]
    assert codes_a[3] == 429
    assert client.get("/api/ping", headers={"Authorization": f"Bearer {tok_b}"}).status_code == 200


def test_ratelimit_anonymous_by_ip(client, monkeypatch):
    """匿名请求仍按 IP 分桶: 第 4 次被限(修复不改变匿名行为)。"""
    monkeypatch.setenv("RATE_LIMIT_GET", "3")
    codes = [client.get("/api/ping").status_code for _ in range(4)]
    assert codes[0] == 200
    assert codes[3] == 429
