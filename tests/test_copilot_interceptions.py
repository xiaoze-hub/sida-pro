"""事中拦截可见化回归（预算/并发）。

铁律：
  - 只回查**已发生**的拦截记录；没有记录 = 没被拦过，**不**代表"不会拦"；
  - 只查本人（别人的拦截一条都看不到）；
  - 单条 run 的治理视图里带上它自己的拦截记录，前端可标"被拦过"。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import AuditLog, Base, CopilotRun
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
    db.add(CopilotRun(run_id="r-hit", user_id="u1", recipe="deep_diagnosis",
                      question="诊断", status="failed", tool_calls=12,
                      created_at=datetime.utcnow()))
    db.add(CopilotRun(run_id="r-clean", user_id="u1", recipe="deep_diagnosis",
                      question="诊断2", status="ok", tool_calls=4,
                      created_at=datetime.utcnow()))
    db.add(AuditLog(user_id="u1", username="", action="copilot_tool_budget_exceeded",
                    detail="run=r-hit used=12 limit=11"))
    db.add(AuditLog(user_id="u1", username="", action="copilot_concurrency_exceeded",
                    detail="并发上限 2, 当前 3"))
    db.add(AuditLog(user_id="other", username="", action="copilot_tool_budget_exceeded",
                    detail="run=r-hit used=99 limit=11"))
    db.commit()
    db.close()

    app = FastAPI()
    app.include_router(copilot_workbench.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: Session()
    app.dependency_overrides[get_current_user] = lambda: _User()
    return TestClient(app)


def test_run治理视图带自己的拦截记录(env):
    body = env.get("/api/copilot/runs/r-hit/governance").json()
    assert len(body["interceptions"]) == 1
    assert body["interceptions"][0]["action"] == "copilot_tool_budget_exceeded"
    assert "used=12" in body["interceptions"][0]["detail"]


def test_没被拦过的run是空列表(env):
    body = env.get("/api/copilot/runs/r-clean/governance").json()
    assert body["interceptions"] == []


def test_拦截列表只含本人(env):
    body = env.get("/api/copilot/governance/interceptions").json()
    assert body["count"] == 2
    # 别人的那条(used=99)不许出现
    assert not any("used=99" in i["detail"] for i in body["items"])
    assert {i["action"] for i in body["items"]} == {
        "copilot_tool_budget_exceeded", "copilot_concurrency_exceeded"
    }


def test_limit参数生效且被夹紧(env):
    assert env.get("/api/copilot/governance/interceptions?limit=1").json()["count"] == 1
    # 非法大值不报错, 夹到上限
    assert env.get("/api/copilot/governance/interceptions?limit=999").json()["count"] == 2
