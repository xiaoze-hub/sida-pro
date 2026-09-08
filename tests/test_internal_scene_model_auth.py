"""风险方案 0.0 回归: /api/service/forecast-config 仅服务令牌可读。

该端点下发明文 api_key(referee 场景绑定 + forecast_llm_*), 依赖已从
get_user_or_service 收紧为 get_service_principal —— 用户 JWT(哪怕 admin)
一律 403, 明文密钥绝不因为"某人登录了"就下发。
隔离手法照抄 tests/test_p1_service_token.py(env 注入 + _service_token 置 None)。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M  # noqa: F401  (确保表注册到 Base)
from src.web.api import service_config
from src.web.api.auth import (
    SERVICE_TOKEN_HEADER,
    ServicePrincipal,
    create_token,
    get_service_principal,
    get_service_token,
)
from src.web.database import Base, get_db

_SERVICE_TOKEN = "t0-forecast-config-token"


class _Req:
    def __init__(self, headers):
        self.headers = headers


def _app(monkeypatch):
    monkeypatch.setenv("SIDA_SERVICE_TOKEN", _SERVICE_TOKEN)
    import src.web.api.auth as auth_mod

    auth_mod._service_token = None  # 隔离: 强迫重读本次 env
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(service_config.router, prefix="/api/service")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False), Session


def _seed_llm_settings(Session):
    from src.web.models import AppSettings

    s = Session()
    s.add(AppSettings(key="forecast_llm_base_url", value="https://llm.example/v1"))
    s.add(AppSettings(key="forecast_llm_model", value="m-sentiment"))
    s.add(AppSettings(key="forecast_llm_api_key", value="sk-sentiment"))
    s.commit()
    s.close()


def test_service_token_value(monkeypatch):
    _app(monkeypatch)
    assert get_service_token() == _SERVICE_TOKEN


def test_get_service_principal_accepts_valid_token(monkeypatch):
    _app(monkeypatch)
    p = asyncio.run(get_service_principal(_Req({SERVICE_TOKEN_HEADER: _SERVICE_TOKEN})))
    assert isinstance(p, ServicePrincipal)
    assert p.is_service


def test_get_service_principal_rejects_without_header(monkeypatch):
    _app(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(get_service_principal(_Req({})))
    assert ei.value.status_code == 403


def test_correct_service_token_reads_config(monkeypatch):
    client, Session = _app(monkeypatch)
    _seed_llm_settings(Session)
    r = client.get(
        "/api/service/forecast-config", headers={SERVICE_TOKEN_HEADER: _SERVICE_TOKEN}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["llm"]["api_key"] == "sk-sentiment"
    assert body["llm"]["model"] == "m-sentiment"
    assert body["referee"] is None  # 无绑定 → 引擎侧 abstain, 不回落硬编码


def test_wrong_service_token_is_403(monkeypatch):
    client, Session = _app(monkeypatch)
    r = client.get(
        "/api/service/forecast-config", headers={SERVICE_TOKEN_HEADER: "wrong"}
    )
    assert r.status_code == 403, r.text
    assert "api_key" not in r.text


def test_missing_token_is_403(monkeypatch):
    client, Session = _app(monkeypatch)
    r = client.get("/api/service/forecast-config")
    assert r.status_code == 403, r.text
    assert "api_key" not in r.text


def test_valid_user_jwt_is_403(monkeypatch):
    """哪怕合法 admin JWT 也拿不到密钥(0.0 核心收紧点)。"""
    client, Session = _app(monkeypatch)
    _seed_llm_settings(Session)
    import src.web.api.auth as auth_mod
    from src.web.models import User

    monkeypatch.setattr(auth_mod, "get_jwt_secret", lambda: "unit-test-jwt-secret")
    s = Session()
    u = User(id="u-admin-1", username="admin", password_hash="x", role="owner")
    s.add(u)
    s.commit()
    token, _ = create_token(u)
    s.close()

    r = client.get(
        "/api/service/forecast-config", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403, r.text
    assert "api_key" not in r.text
