"""经验库只读端点（P2-2.4）。

钉住四件事：
  1. 只读：不提供写入/改判 verdict 的入口；
  2. 隔离：普通账号只看自己发起的，admin 看全量；
  3. 诚实：非法 verdict 如实报错；证据文件不存在要标 `evidence_exists=False`；
  4. 不补 0：baseline/candidate 缺的键原样返回，不填 0。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import Base, HarnessExperiment
from src.web.api import copilot as copilot_api
from src.web.api.auth import get_current_user
from src.web.database import get_db


class _User:
    def __init__(self, uid: str, admin: bool = False):
        self.id = uid
        self.username = uid
        self.is_admin = admin
        self.role = "admin" if admin else "user"


@pytest.fixture()
def make_client():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    HarnessExperiment.__table__.create(eng, checkfirst=True)
    Session = sessionmaker(bind=eng)

    db = Session()
    db.add(HarnessExperiment(user_id="u1", recipe_id="deep_diagnosis",
                             change_summary="补一句必须带口径", verdict="keep",
                             baseline={"pass_rate": 0.5, "cost": None},
                             candidate={"pass_rate": 0.75},
                             reasons=[], evidence_path="/tmp/not-exist-xyz.json",
                             created_at=datetime.utcnow()))
    db.add(HarnessExperiment(user_id="u2", recipe_id="theme_leader",
                             change_summary="删掉重复取数", verdict="rollback",
                             baseline={"pass_rate": 0.8}, candidate={"pass_rate": 0.6},
                             reasons=["pass_rate 下降"], created_at=datetime.utcnow()))
    db.commit()
    db.close()

    def _factory(user: _User):
        app = FastAPI()
        app.include_router(copilot_api.router, prefix="/api")
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db] = lambda: Session()
        return TestClient(app)

    return _factory


def test_普通账号只看自己的实验(make_client):
    c = make_client(_User("u1"))
    body = c.get("/api/copilot/experiments").json()
    assert body["available"] is True
    assert body["total"] == 1
    assert body["items"][0]["recipe_id"] == "deep_diagnosis"


def test_admin看全量(make_client):
    c = make_client(_User("admin", admin=True))
    body = c.get("/api/copilot/experiments").json()
    assert body["total"] == 2
    assert {i["verdict"] for i in body["items"]} == {"keep", "rollback"}


def test_verdict过滤(make_client):
    c = make_client(_User("admin", admin=True))
    body = c.get("/api/copilot/experiments?verdict=rollback").json()
    assert body["total"] == 1
    assert body["items"][0]["verdict"] == "rollback"


def test_非法verdict如实报错(make_client):
    c = make_client(_User("admin", admin=True))
    body = c.get("/api/copilot/experiments?verdict=maybe").json()
    assert body["available"] is False
    assert "keep/rollback" in body["reason"]


def test_详情不补0且标注证据缺失(make_client):
    c = make_client(_User("u1"))
    body = c.get("/api/copilot/experiments/1").json()
    item = body["item"]
    assert item["baseline"]["cost"] is None, "缺值必须原样是 null，不能填 0"
    assert "cost" not in item["candidate"], "候选没有的键不许凭空补出来"
    assert item["evidence_path"] == "/tmp/not-exist-xyz.json"
    assert item["evidence_exists"] is False, "文件不存在就要标 False，不能让前端以为能下载"


def test_详情找不到如实说(make_client):
    c = make_client(_User("u1"))
    body = c.get("/api/copilot/experiments/9999").json()
    assert body["available"] is False
    assert "没找到" in body["reason"]


def test_别人的实验看不到(make_client):
    c = make_client(_User("u1"))
    assert c.get("/api/copilot/experiments/2").json()["available"] is False
