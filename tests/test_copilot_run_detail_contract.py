"""详情接口必须回 conversation_id（2026-10-25 生产实测）。

后台执行完成后，前端要拿 `conversation_id` 去重拉会话读正文。这个字段之前**根本不在
详情响应里**（只有 run_id/status/recipe/question/tool_calls/elapsed_ms/created_at），
前端只能读到 null，于是每次都提示「已完成，但没拿到会话 id」。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import Base, CopilotRun, CopilotStep
from src.web.api import copilot as copilot_api
from src.web.api.auth import get_current_user
from src.web.database import get_db


class _User:
    id = "u1"
    username = "tester"


@pytest.fixture()
def client(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for t in (CopilotRun.__table__, CopilotStep.__table__):
        t.create(eng, checkfirst=True)
    Session = sessionmaker(bind=eng)

    db = Session()
    db.add(CopilotRun(run_id="r-detail", user_id="u1", status="ok",
                      question="问一句", tool_calls=2, elapsed_ms=1234,
                      conversation_id=777, created_at=datetime.utcnow()))
    db.add(CopilotStep(run_id="r-detail", seq=1, tool="get_range_summary", ok=True, elapsed_ms=12))
    db.commit()
    db.close()

    app = FastAPI()
    app.include_router(copilot_api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _User()
    app.dependency_overrides[get_db] = lambda: Session()
    with TestClient(app) as c:
        yield c


def test_详情回_conversation_id(client):
    r = client.get("/api/copilot/runs/r-detail")
    assert r.status_code == 200
    run = r.json()["run"]
    assert run["conversation_id"] == 777, "前端要靠它重拉会话，缺了就只能提示'没拿到会话 id'"


def test_详情仍带原有字段与步骤(client):
    run = client.get("/api/copilot/runs/r-detail").json()
    for k in ("run_id", "status", "question", "tool_calls", "elapsed_ms", "created_at"):
        assert k in run["run"], f"丢字段 {k}"
    assert len(run["steps"]) == 1
    assert run["steps"][0]["tool"] == "get_range_summary"
