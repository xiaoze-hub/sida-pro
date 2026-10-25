"""P1-1.2 委派预算与并发上限：429 带数字 / 预算超限中止 / 审计 / 未超限放行。

时序纪律（沿用 tests/test_copilot_jobs.py 的教训）：
**不靠 sleep 等后台任务**。预算巡检的间隔 `BUDGET_POLL_S` 在这里被压到 0，
中止点是"执行体写入第 N+1 条 copilot_steps 之后必定被巡检看到"，由事件循环调度决定，
不是墙钟；外层再加 `wait_for` 兜底防止挂死（那是保险丝，不是等待手段）。

sqlite 内存库必须 StaticPool：TestClient 在另一个线程跑端点，新连接会拿到另一个空库。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import copilot_quota as quota
from src.db.models import AuditLog, CopilotRun, CopilotStep
from src.web.api import copilot_jobs
from src.web.api.auth import get_current_user


class _User:
    def __init__(self, uid="u1"):
        self.id = uid
        self.username = "tester"


@pytest.fixture()
def Session(monkeypatch):
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (CopilotRun.__table__, CopilotStep.__table__, AuditLog.__table__):
        table.create(eng)
    factory = sessionmaker(bind=eng)

    import src.db.session as session_mod
    monkeypatch.setattr(session_mod, "SessionLocal", factory)
    copilot_jobs._TASKS.clear()
    return factory


@pytest.fixture()
def client(Session):
    app = FastAPI()
    app.include_router(copilot_jobs.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _User("u1")
    with TestClient(app) as c:
        yield c


# ── 小工具 ────────────────────────────────────────────────────────────────
def _mk_run(Session, run_id, status="queued", user_id="u1", tool_calls=0):
    db = Session()
    try:
        db.add(CopilotRun(run_id=run_id, user_id=user_id, status=status,
                          question="q", tool_calls=tool_calls))
        db.commit()
    finally:
        db.close()


def _row(Session, run_id):
    db = Session()
    try:
        return db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
    finally:
        db.close()


def _audits(Session):
    db = Session()
    try:
        return db.query(AuditLog).order_by(AuditLog.id).all()
    finally:
        db.close()


def _add_steps(Session, run_id, n):
    db = Session()
    try:
        for i in range(1, n + 1):
            db.add(CopilotStep(run_id=run_id, seq=i, tool=f"tool_{i}", ok=True))
        db.commit()
    finally:
        db.close()


def _spy_audit(monkeypatch):
    """包住真实写入的探针：既能 assert 被调用，又保留真实落库可用于二次校验。"""
    calls = []
    real = quota.audit_limit_hit

    def spy(db, **kw):
        calls.append(kw)
        return real(db, **kw)

    monkeypatch.setattr(quota, "audit_limit_hit", spy)
    return calls


# ── 1. 并发上限 ───────────────────────────────────────────────────────────
def test_并发超限返429且错误体带现有数量与上限(client, Session, monkeypatch):
    monkeypatch.setattr(quota, "MAX_ACTIVE_RUNS_PER_USER", 2)
    _mk_run(Session, "r-a", status="queued")
    _mk_run(Session, "r-b", status="running")

    r = client.post("/api/copilot/runs", json={"question": "再问一遍"})

    assert r.status_code == 429
    detail = r.json()["detail"]
    assert detail["code"] == "copilot_concurrency_exceeded"
    assert detail["active"] == 2, f"必须带现有数量，实际 {detail}"
    assert detail["limit"] == 2, f"必须带上限，实际 {detail}"
    assert "2" in detail["message"]

    # 被拒时**不能**留下一条新 run（否则拒了还在排队里加人）
    db = Session()
    try:
        assert db.query(CopilotRun).count() == 2
    finally:
        db.close()


def test_并发超限写审计且探针被调用一次(client, Session, monkeypatch):
    calls = _spy_audit(monkeypatch)
    monkeypatch.setattr(quota, "MAX_ACTIVE_RUNS_PER_USER", 1)
    _mk_run(Session, "r-only", status="running")

    assert client.post("/api/copilot/runs", json={"question": "x"}).status_code == 429

    assert len(calls) == 1, f"审计必须被调用且只调一次，实际 {calls}"
    assert calls[0]["action"] == "copilot_concurrency_exceeded"
    assert calls[0]["user_id"] == "u1"
    rows = _audits(Session)
    assert [a.action for a in rows] == ["copilot_concurrency_exceeded"]
    assert rows[0].user_id == "u1"


def test_未超限正常提交(client, Session, monkeypatch):
    async def quick(**_kw):
        return 7

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", quick)
    monkeypatch.setattr(quota, "MAX_ACTIVE_RUNS_PER_USER", 2)
    _mk_run(Session, "r-busy", status="running")   # 1 个在跑，还没到 2

    r = client.post("/api/copilot/runs", json={"question": "上证多少点"})

    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    assert _row(Session, r.json()["run_id"]) is not None
    assert _audits(Session) == []
    for t in list(copilot_jobs._TASKS.values()):
        t.cancel()


def test_别人的run不占我的并发额度(client, Session, monkeypatch):
    monkeypatch.setattr(quota, "MAX_ACTIVE_RUNS_PER_USER", 1)
    _mk_run(Session, "r-u2", status="running", user_id="u2")

    async def quick(**_kw):
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", quick)
    assert client.post("/api/copilot/runs", json={"question": "x"}).status_code == 200
    for t in list(copilot_jobs._TASKS.values()):
        t.cancel()


# ── 2. 工具预算 ───────────────────────────────────────────────────────────
def test_预算超限中止并标failed_steps计数为准(Session, monkeypatch):
    calls = _spy_audit(monkeypatch)
    monkeypatch.setattr(quota, "MAX_TOOL_CALLS_PER_RUN", 3)
    monkeypatch.setattr(copilot_jobs, "BUDGET_POLL_S", 0)   # 确定性：巡检不睡

    async def runaway(**_kw):
        # 模拟工具层失控：连写 5 条步骤后**永不返回**（真实场景里是继续一轮轮取数）
        _add_steps(Session, "r-budget", 5)
        await asyncio.Event().wait()

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", runaway)
    _mk_run(Session, "r-budget")

    async def drive():
        await asyncio.wait_for(
            copilot_jobs._execute("r-budget", user_id="u1", conversation_id=None, question="q"),
            timeout=10,
        )

    asyncio.run(drive())

    row = _row(Session, "r-budget")
    assert row.status == "failed", f"超预算必须中止，实际 status={row.status}"
    assert "预算" in (row.error or ""), row.error
    assert "5" in row.error and "3" in row.error, "错误里要写清已用与上限"
    assert row.tool_calls == 5, "中止时要把真实调用次数记下来，不是 0"
    assert row.finished_at is not None

    assert len(calls) == 1 and calls[0]["action"] == "copilot_tool_budget_exceeded"
    assert "used=5" in calls[0]["detail"] and "limit=3" in calls[0]["detail"]
    assert [a.action for a in _audits(Session)] == ["copilot_tool_budget_exceeded"]


def test_预算恰好等于上限不算超(Session, monkeypatch):
    """边界：used == limit 放行，> 才中止（否则 60 次预算是白送一次）。"""
    monkeypatch.setattr(quota, "MAX_TOOL_CALLS_PER_RUN", 3)
    monkeypatch.setattr(copilot_jobs, "BUDGET_POLL_S", 0)

    async def at_limit(**_kw):
        _add_steps(Session, "r-edge", 3)
        return 12

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", at_limit)
    _mk_run(Session, "r-edge")

    asyncio.run(copilot_jobs._execute("r-edge", user_id="u1", conversation_id=None, question="q"))

    row = _row(Session, "r-edge")
    assert row.status == "ok", row.error
    assert _audits(Session) == []


def test_check_run_budget_取steps与tool_calls的大者(Session, monkeypatch):
    monkeypatch.setattr(quota, "MAX_TOOL_CALLS_PER_RUN", 10)
    _mk_run(Session, "r-cnt", status="running", tool_calls=8)
    _add_steps(Session, "r-cnt", 2)

    db = Session()
    try:
        assert quota.run_tool_calls(db, "r-cnt") == 8
        assert quota.check_run_budget(db, "r-cnt") == (True, 8, 10)
        assert quota.check_run_budget(db, "nope") == (True, 0, 10)
    finally:
        db.close()


def test_未超预算的执行体照常成功(Session, monkeypatch):
    monkeypatch.setattr(quota, "MAX_TOOL_CALLS_PER_RUN", 60)
    monkeypatch.setattr(copilot_jobs, "BUDGET_POLL_S", 0)

    async def fine(**_kw):
        _add_steps(Session, "r-ok", 2)
        return 33

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", fine)
    _mk_run(Session, "r-ok")

    asyncio.run(copilot_jobs._execute("r-ok", user_id="u1", conversation_id=None, question="q"))

    row = _row(Session, "r-ok")
    assert row.status == "ok"
    assert row.conversation_id == 33
    assert _audits(Session) == []


def test_审计写失败不影响返回429(client, Session, monkeypatch):
    """审计是 best-effort：写不进去也要照常 429（否则限流变成 500）。"""
    monkeypatch.setattr(quota, "MAX_ACTIVE_RUNS_PER_USER", 1)
    _mk_run(Session, "r-x", status="running")

    def boom(*_a, **_kw):
        raise RuntimeError("audit_logs 表不可写")

    monkeypatch.setattr(quota, "audit_limit_hit", boom)

    r = client.post("/api/copilot/runs", json={"question": "x"})
    assert r.status_code == 429
    assert r.json()["detail"]["active"] == 1
