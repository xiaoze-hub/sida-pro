"""运行治理可见化（GET /copilot/runs/{id}/governance）回归。

口径红线：
  - 预算/用量**缺一边就报 None**，不按 0 兜底（没记到 ≠ 用了 0 次）；
  - 白名单为空 = "未限定"，不是"允许所有"，前端据此显示；
  - Frozen Ring 四条固定列出，且本接口**只读**（不参与拦截）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import Base, CopilotRun
from src.web.api import copilot_workbench
from src.web.api.auth import get_current_user
from src.web.database import get_db


class _User:
    id = "u1"
    username = "tester"
    role = "member"
    permissions = {}


@pytest.fixture()
def env():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False)

    db = Session()
    db.add(CopilotRun(run_id="r-ok", user_id="u1", recipe="deep_diagnosis",
                      question="诊断", status="ok", tool_calls=3,
                      created_at=datetime.utcnow()))
    db.add(CopilotRun(run_id="r-norecipe", user_id="u1", recipe=None,
                      question="闲聊", status="ok", tool_calls=None,
                      created_at=datetime.utcnow()))
    db.add(CopilotRun(run_id="r-other", user_id="other", recipe=None,
                      question="x", status="ok", created_at=datetime.utcnow()))
    db.commit()
    db.close()

    app = FastAPI()
    app.include_router(copilot_workbench.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: Session()
    app.dependency_overrides[get_current_user] = lambda: _User()
    return TestClient(app)


def test_带配方时给出预算上限与用量(env):
    body = env.get("/api/copilot/runs/r-ok/governance").json()
    assert body["recipe"] == "deep_diagnosis"
    assert isinstance(body["budget"]["max"], int) and body["budget"]["max"] > 0
    assert body["budget"]["used"] == 3
    assert body["budget"]["remaining"] == body["budget"]["max"] - 3
    assert body["whitelist"]["count"] > 0
    assert body["whitelist"]["count"] == len(body["whitelist"]["tools"])


def test_Frozen_Ring_四条固定列出(env):
    body = env.get("/api/copilot/runs/r-ok/governance").json()
    guards = body["frozen_ring"]["guards"]
    assert len(guards) == 4
    assert any("number_audit" in g for g in guards)
    assert any("caliber" in g for g in guards)
    assert "不得覆盖" in body["frozen_ring"]["note"]


def test_无配方时不编预算(env):
    body = env.get("/api/copilot/runs/r-norecipe/governance").json()
    assert body["recipe"] is None
    assert body["budget"]["max"] is None
    # ⚠️ CopilotRun.tool_calls 列默认 0：库里分不出"没记到"与"真 0 次"，
    # 这里按实际落库值报 0，不强行当 None —— 但**余量**必须 None（缺上限就不能算）
    assert body["budget"]["used"] == 0
    assert body["budget"]["remaining"] is None
    assert body["whitelist"] == {"count": 0, "tools": []}


def test_别人的run一律404(env):
    assert env.get("/api/copilot/runs/r-other/governance").status_code == 404
    assert env.get("/api/copilot/runs/none/governance").status_code == 404


def test_本接口只读_不改run状态(env):
    before = env.get("/api/copilot/runs/r-ok/governance").json()["status"]
    env.get("/api/copilot/runs/r-ok/governance")
    after = env.get("/api/copilot/runs/r-ok/governance").json()["status"]
    assert before == after == "ok"
