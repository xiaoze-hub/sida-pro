"""P0-0.2 参谋长任务异步化：提交即返 run_id / SSE 事件 / 取消 / 超时。

不联网：`run_copilot_question` 被替换成桩，所以测的是**调度与状态机**，
不是 LLM 回答质量（后者由 harness 评测管）。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from src.db.models import CopilotRun, CopilotStep
from src.web.api import copilot_jobs
from src.web.api.auth import get_current_user


class _User:
    def __init__(self, uid="u1"):
        self.id = uid
        self.username = "tester"


@pytest.fixture()
def db_factory(monkeypatch):
    # TestClient 在另一个线程跑端点，sqlite 内存库必须用 StaticPool 共享同一连接，
    # 否则新连接拿到的是另一个空库（报 no such table）
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    CopilotRun.__table__.create(eng)
    CopilotStep.__table__.create(eng)
    Session = sessionmaker(bind=eng)

    import src.db.session as session_mod
    monkeypatch.setattr(session_mod, "SessionLocal", Session)
    # 模块内部是 "from src.db.session import SessionLocal"，需同时替换已导入引用
    return Session


@pytest.fixture()
def client(db_factory):
    app = FastAPI()
    app.include_router(copilot_jobs.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _User("u1")
    with TestClient(app) as c:
        yield c


def _wait_status(Session, run_id, want, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        db = Session()
        try:
            row = db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
            if row and row.status in want:
                return row
        finally:
            db.close()
        time.sleep(0.05)
    raise AssertionError(f"{run_id} 未在 {timeout}s 内变成 {want}")


def test_提交立刻返回_run_id_并落库为_queued(client, db_factory, monkeypatch):
    async def slow(**_kw):
        await asyncio.sleep(0.3)
        return 11

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", slow)
    t0 = time.time()
    r = client.post("/api/copilot/runs", json={"question": "上证现在多少点"})
    cost = time.time() - t0
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["run_id"]
    assert cost < 0.25, f"提交必须立刻返回，实际等了 {cost:.2f}s"

    row = _wait_status(db_factory, body["run_id"], {"queued", "running"})
    assert row.question == "上证现在多少点"


def test_正常跑完变_ok_并回填会话与耗时(client, db_factory, monkeypatch):
    async def ok(**_kw):
        await asyncio.sleep(0.05)
        return 42

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", ok)
    run_id = client.post("/api/copilot/runs", json={"question": "q"}).json()["run_id"]
    row = _wait_status(db_factory, run_id, {"ok"})
    assert row.elapsed_ms is not None and row.elapsed_ms >= 0
    assert row.conversation_id == 42


def test_执行抛异常落成_failed_并带原因(client, db_factory, monkeypatch):
    async def boom(**_kw):
        raise RuntimeError("模型网关 502")

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", boom)
    run_id = client.post("/api/copilot/runs", json={"question": "q"}).json()["run_id"]
    row = _wait_status(db_factory, run_id, {"failed"})
    assert "模型网关 502" in (row.error or "")
    assert row.finished_at is not None


def test_超时标_failed_而不是挂起(client, db_factory, monkeypatch):
    async def hang(**_kw):
        await asyncio.sleep(5)
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", hang)
    monkeypatch.setattr(copilot_jobs, "RUN_TIMEOUT_S", 0.2)
    run_id = client.post("/api/copilot/runs", json={"question": "q"}).json()["run_id"]
    row = _wait_status(db_factory, run_id, {"failed"}, timeout=6)
    assert "超时" in (row.error or "")


def test_取消_running_的_run(client, db_factory, monkeypatch):
    async def hang(**_kw):
        await asyncio.sleep(5)
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", hang)
    run_id = client.post("/api/copilot/runs", json={"question": "q"}).json()["run_id"]
    _wait_status(db_factory, run_id, {"running"})

    r = client.post(f"/api/copilot/runs/{run_id}/cancel")
    assert r.status_code == 200
    row = _wait_status(db_factory, run_id, {"cancelled"}, timeout=3)
    assert row.status == "cancelled"


def test_已完成的不许取消(client, db_factory, monkeypatch):
    async def ok(**_kw):
        return 7

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", ok)
    run_id = client.post("/api/copilot/runs", json={"question": "q"}).json()["run_id"]
    _wait_status(db_factory, run_id, {"ok"})
    r = client.post(f"/api/copilot/runs/{run_id}/cancel")
    assert r.status_code == 409


def test_别人的_run_看不到也取消不了(client, db_factory, monkeypatch):
    async def ok(**_kw):
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", ok)

    # 直接把 run 的 user_id 改成别人
    db = db_factory()
    try:
        db.add(CopilotRun(run_id="other-run", user_id="u2", status="ok",
                          question="q", tool_calls=0))
        db.commit()
    finally:
        db.close()

    assert client.get("/api/copilot/runs/other-run/events").status_code == 404
    assert client.post("/api/copilot/runs/other-run/cancel").status_code == 404


def test_SSE_会推状态并在终态关闭(client, db_factory, monkeypatch):
    async def ok(**_kw):
        return 9

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", ok)
    run_id = client.post("/api/copilot/runs", json={"question": "q"}).json()["run_id"]
    _wait_status(db_factory, run_id, {"ok"})

    text = client.get(f"/api/copilot/runs/{run_id}/events").text
    assert "event: status" in text
    assert "event: done" in text
    assert '"status": "ok"' in text or '"status":"ok"' in text


def test_SSE_对不存在的_run_返回404(client):
    assert client.get("/api/copilot/runs/nope/events").status_code == 404


def test_空问题被拒(client):
    r = client.post("/api/copilot/runs", json={"question": ""})
    assert r.status_code == 422
