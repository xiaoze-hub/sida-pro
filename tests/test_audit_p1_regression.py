"""审计回归: P1-7 掩码拒收 + P2-1 白名单 + P1-13 百分点 + P1-11 单例指纹。"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M  # noqa: F401
from src.web.api import settings as settings_api
from src.web.database import Base, get_db


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(settings_api.router, prefix="/settings")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def test_secret_mask_rejected(tmp_path, monkeypatch):
    """P1-7: PUT 掩码值 400 拒收, 不毁凭证。"""
    c = _client(tmp_path, monkeypatch)
    r = c.put("/settings/ths_sdk_password", json={"value": "********"})
    assert r.status_code == 400, r.text
    r = c.put("/settings/ths_sdk_password", json={"value": "real_pw"})
    assert r.status_code == 200, r.text


def test_unknown_key_rejected(tmp_path, monkeypatch):
    """P2-1: 非白名单/保留键拒绝直写(400 或 403 均可, 关键是不 200)。"""
    c = _client(tmp_path, monkeypatch)
    r = c.put("/settings/jwt_secret", json={"value": "x"})
    assert r.status_code in (400, 403), r.text
    r = c.put("/settings/not_a_key", json={"value": "x"})
    assert r.status_code == 400, r.text


@pytest.fixture(autouse=True)
def _mock_thsdk_module():
    fake = MagicMock()
    fake.THS = MagicMock()
    fake.THSResponse = MagicMock()
    sys.modules["thsdk"] = fake
    yield
    sys.modules.pop("thsdk", None)


def test_singleton_rebuilds_on_cred_change(monkeypatch):
    """P1-11: 凭据指纹变化 → 单例重建, 不冻结游客。"""
    import data_source.thsdk_l2 as L

    L._default_client = None
    L._default_client_fingerprint = ("", "")
    L._CRED_CACHE.update(ts=0.0, username="", password="")
    monkeypatch.setattr(L, "_db_ths_creds", lambda: ("mx_new", "pw_new"))
    c1 = L._get_default_client()
    assert c1.username == "mx_new" and not c1._is_guest
    # 同凭据 → 同实例
    assert L._get_default_client() is c1
    # 换凭据 → 重建
    monkeypatch.setattr(L, "_db_ths_creds", lambda: ("mx_v2", "pw_v2"))
    L._CRED_CACHE.update(ts=0.0, username="", password="")
    c2 = L._get_default_client()
    assert c2 is not c1 and c2.username == "mx_v2"
    L._default_client = None
    L._default_client_fingerprint = ("", "")
