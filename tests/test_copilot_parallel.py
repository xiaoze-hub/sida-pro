"""并行诊断（多子 run）回归。

钉四件事：
  1. 校验在前：空清单 / 超过 4 个 → 400，不建父 run；
  2. 提交即返回（不在请求里等 LLM）：父 run 立刻落库，状态是 queued/running；
  3. 汇总只报事实：ok/failed/pending 计数 + 缺口文案，不"替子任务重跑"；
  4. 别人的 run 一律 404（不泄露存在性）。

注意：**不断言后台执行完成**（那要等真 LLM）。执行体由 `delegate_many` 注入，
本文件只验接口契约与汇总口径。
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
from src.web.api import copilot_workbench
from src.web.api.auth import get_current_user
from src.web.database import get_db


class _User:
    id = "u1"
    username = "tester"
    role = "member"
    permissions = {}


@pytest.fixture()
def env(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False)

    # 后台执行体整个换掉：本文件只验契约，不跑 LLM
    def _fake_delegate_many(db, *, parent_run_id, user_id, tasks, executor=None, budget=None):
        kids = []
        for recipe_id, question in tasks:
            kid_id = f"{parent_run_id[:8]}-{len(kids)}"
            db.add(
                CopilotRun(
                    run_id=kid_id,
                    parent_run_id=parent_run_id,
                    user_id=user_id,
                    recipe=None,
                    question=question,
                    status="ok",
                    tool_calls=2,
                    elapsed_ms=1200,
                    created_at=datetime.utcnow(),
                )
            )
            kids.append({"run_id": kid_id, "status": "ok"})
        db.commit()
        return {"children": kids}

    monkeypatch.setattr("src.core.delegate.delegate_many", _fake_delegate_many)
    # 接口内部用的是 SessionLocal(与 copilot_jobs 同款, 为后台线程开独立会话),
    # 不 patch 就会写到真实库 —— 单测必须把它指到本用例的内存库。
    monkeypatch.setattr("src.db.session.SessionLocal", Session)

    app = FastAPI()
    app.include_router(copilot_workbench.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: Session()
    app.dependency_overrides[get_current_user] = lambda: _User()
    return TestClient(app), Session


def test_空清单被拒(env):
    client, _ = env
    r = client.post("/api/copilot/parallel", json={"items": []})
    assert r.status_code == 400
    assert "至少要一个子问题" in r.json()["detail"]


def test_超过4个被拒(env):
    client, Session = env
    items = [{"question": f"问题{i}"} for i in range(5)]
    r = client.post("/api/copilot/parallel", json={"items": items})
    assert r.status_code == 400
    assert "最多 4 个" in r.json()["detail"]

    db = Session()
    try:
        # 校验失败不能留下父 run
        assert db.query(CopilotRun).count() == 0
    finally:
        db.close()


def test_提交即返回并落库父run(env):
    client, Session = env
    r = client.post(
        "/api/copilot/parallel",
        json={"items": [{"question": "600111 现在能追吗"}, {"question": "今天大盘情绪如何"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert body["run_id"]

    db = Session()
    try:
        row = db.query(CopilotRun).filter(CopilotRun.run_id == body["run_id"]).first()
        assert row is not None
        assert row.recipe == "parallel"
        assert row.status in ("queued", "running", "ok", "failed")
    finally:
        db.close()


def test_汇总只报事实与缺口(env):
    client, Session = env
    db = Session()
    db.add(CopilotRun(run_id="p1", user_id="u1", recipe="parallel",
                      question="并行诊断 · 3 个子任务", status="ok",
                      created_at=datetime.utcnow()))
    db.add(CopilotRun(run_id="c1", parent_run_id="p1", user_id="u1", recipe="chip_analyst",
                      question="筹码如何", status="ok", tool_calls=3, elapsed_ms=900,
                      created_at=datetime.utcnow()))
    db.add(CopilotRun(run_id="c2", parent_run_id="p1", user_id="u1", recipe="risk_judge",
                      question="风险大吗", status="failed", error="上游超时",
                      created_at=datetime.utcnow()))
    db.commit()
    db.close()

    r = client.get("/api/copilot/parallel/p1")
    assert r.status_code == 200
    s = r.json()["summary"]
    assert s["available"] is True
    assert s["count"] == 2
    assert s["ok"] == 1
    assert s["failed"] == 1
    assert s["tool_calls"] == 3  # 只累计成功的子 run
    assert any("risk_judge" in g and "上游超时" in g for g in s["gaps"])


def test_没有子任务时available为False而不是报错(env):
    client, Session = env
    db = Session()
    db.add(CopilotRun(run_id="p2", user_id="u1", recipe="parallel", question="x",
                      status="queued", created_at=datetime.utcnow()))
    db.commit()
    db.close()

    body = client.get("/api/copilot/parallel/p2").json()
    assert body["summary"]["available"] is False
    assert body["summary"]["reason"] == "这轮没有委派子任务"


def test_别人的run返回404(env):
    client, Session = env
    db = Session()
    db.add(CopilotRun(run_id="p3", user_id="other-user", recipe="parallel", question="x",
                      status="ok", created_at=datetime.utcnow()))
    db.commit()
    db.close()
    assert client.get("/api/copilot/parallel/p3").status_code == 404
