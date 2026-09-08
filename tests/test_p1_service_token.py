"""P1 成熟化回归: 用户 JWT / 服务 token 双轨(只读行情口)。

- 无任何凭证调 klines → 401(保持现状, 未放开匿名)
- X-Service-Token 调 klines → 非 401(依赖放行; 联网失败则 5xx/200 均可, 关键不是 401)
- 服务 token 调写口(POST /api/accounts) → 401/403(提权面为零)
- 服务 token 调 owner 口(GET /api/audit) → 403(进不了 require_owner)
- 用户 JWT 行为不变: 无 token 调写口 → 401
"""
from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M  # noqa: F401
from src.web.api import klines as klines_api
from src.web.api import accounts as accounts_api
from src.web.api import audit as audit_api
from src.web.api.auth import (
    SERVICE_TOKEN_HEADER,
    get_service_token,
    get_user_or_service,
)
from src.web.database import Base, get_db


def _app(monkeypatch) -> TestClient:
    monkeypatch.setenv("SIDA_SERVICE_TOKEN", "p1-test-service-token")
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
    app.include_router(klines_api.router, prefix="/klines", dependencies=[Depends(get_user_or_service)])
    app.include_router(accounts_api.router, prefix="/accounts-acc")
    app.include_router(audit_api.router, prefix="/audit")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app, raise_server_exceptions=False)


def test_service_token_value(monkeypatch):
    _app(monkeypatch)
    assert get_service_token() == "p1-test-service-token"


def test_klines_no_cred_is_401(monkeypatch):
    c = _app(monkeypatch)
    r = c.get("/klines/600519", params={"market": "CN", "days": 5})
    assert r.status_code == 401, r.text


def test_klines_service_token_not_401(monkeypatch):
    c = _app(monkeypatch)
    r = c.get(
        "/klines/600519",
        params={"market": "CN", "days": 5},
        headers={SERVICE_TOKEN_HEADER: "p1-test-service-token"},
    )
    assert r.status_code != 401, r.text


def test_klines_wrong_service_token_is_401(monkeypatch):
    c = _app(monkeypatch)
    r = c.get(
        "/klines/600519",
        params={"market": "CN", "days": 5},
        headers={SERVICE_TOKEN_HEADER: "wrong"},
    )
    assert r.status_code == 401, r.text


def test_write_endpoint_rejects_service_token(monkeypatch):
    """服务 token 进写口: accounts 全挂 protected → 无 Bearer 直接 401。"""
    c = _app(monkeypatch)
    r = c.post(
        "/accounts-acc/accounts",
        json={"name": "x", "available_funds": 1},
        headers={SERVICE_TOKEN_HEADER: "p1-test-service-token"},
    )
    assert r.status_code in (401, 403), r.text


def test_owner_endpoint_rejects_service_token(monkeypatch):
    """服务 token 进 owner 口 → 403, 提权面为零(行为: 无用户JWT先401于get_current_user,
    require_owner 包一层; 任一非200即达标, 关键是拿不到数据)。"""
    c = _app(monkeypatch)
    r = c.get("/audit", headers={SERVICE_TOKEN_HEADER: "p1-test-service-token"})
    assert r.status_code in (401, 403), r.text


def test_write_endpoint_no_cred_is_401(monkeypatch):
    c = _app(monkeypatch)
    r = c.post("/accounts-acc/accounts", json={"name": "x", "available_funds": 1})
    assert r.status_code == 401, r.text
