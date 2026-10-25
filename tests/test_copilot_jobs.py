"""P0-0.2 参谋长任务异步化：提交即返 run_id / SSE 事件 / 取消 / 超时。

不联网：`run_copilot_question` 被替换成桩，测的是**调度与状态机**。

⚠️ 时序纪律（2026-10-25 CI 踩过）：**不要靠 sleep 等后台任务完成**。
本地 0.05s 就好的用例，在 CI 慢机上 5s 都等不到 —— 那不是产品 bug，是测试写法错。
这里改成两条互不依赖的断言：
  · 「提交立即返回且任务已登记」→ 看 `_TASKS`（create_task 真的调了）
  · 「状态机正确」→ 直接 `asyncio.run(_execute(...))` 驱动，完全确定
"""

from __future__ import annotations

import asyncio

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
def Session(monkeypatch):
    # TestClient 在另一个线程跑端点，sqlite 内存库必须 StaticPool 共享连接，
    # 否则新连接拿到另一个空库（报 no such table）
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    CopilotRun.__table__.create(eng)
    CopilotStep.__table__.create(eng)
    session_factory = sessionmaker(bind=eng)

    import src.db.session as session_mod
    monkeypatch.setattr(session_mod, "SessionLocal", session_factory)
    copilot_jobs._TASKS.clear()
    return session_factory


@pytest.fixture()
def client(Session):
    app = FastAPI()
    app.include_router(copilot_jobs.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _User("u1")
    with TestClient(app) as c:
        yield c


def _row(Session, run_id):
    db = Session()
    try:
        return db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
    finally:
        db.close()


def _mk_run(Session, run_id="r1", status="queued", user_id="u1"):
    db = Session()
    try:
        db.add(CopilotRun(run_id=run_id, user_id=user_id, status=status,
                          question="q", tool_calls=0))
        db.commit()
    finally:
        db.close()


# ── 提交 ──────────────────────────────────────────────────────────────────
def test_提交立刻返回且任务已登记(client, Session, monkeypatch):
    async def slow(**_kw):
        await asyncio.sleep(30)
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", slow)
    body = client.post("/api/copilot/runs", json={"question": "上证现在多少点"}).json()

    assert body["status"] == "queued"
    assert body["run_id"] in copilot_jobs._TASKS, "create_task 必须真的被调用（不是假异步）"
    assert _row(Session, body["run_id"]).question == "上证现在多少点"

    # 清理：别让 30s 的桩任务挂在测试进程里
    for t in list(copilot_jobs._TASKS.values()):
        t.cancel()


def test_空问题被拒(client):
    assert client.post("/api/copilot/runs", json={"question": ""}).status_code == 422


def test_执行体必须把_run_id_传给执行函数(Session, monkeypatch):
    """2026-10-25 生产实测：签名加了 run_id、**调用点忘了传** → 依然重复建轨迹。
    这条用例专门拦"改了签名没改调用"。"""
    seen = {}

    async def spy(**kw):
        seen.update(kw)
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", spy)
    _mk_run(Session, "r-fwd")

    asyncio.run(copilot_jobs._execute("r-fwd", user_id="u1", conversation_id=None, question="q"))

    assert seen.get("run_id") == "r-fwd", f"run_id 没传给执行函数，kwargs={list(seen)}"


# ── 状态机（直接驱动执行体，不等调度）──────────────────────────────────────
def test_执行完成写_ok_并回填会话与耗时(Session, monkeypatch):
    async def ok(**_kw):
        return 42

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", ok)
    _mk_run(Session, "r-ok")

    asyncio.run(copilot_jobs._execute("r-ok", user_id="u1", conversation_id=None, question="q"))

    row = _row(Session, "r-ok")
    assert row.status == "ok"
    assert row.conversation_id == 42
    assert row.elapsed_ms is not None and row.elapsed_ms >= 0
    assert row.finished_at is not None


def test_执行抛异常落成_failed_并带原因(Session, monkeypatch):
    async def boom(**_kw):
        raise RuntimeError("模型网关 502")

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", boom)
    _mk_run(Session, "r-err")

    asyncio.run(copilot_jobs._execute("r-err", user_id="u1", conversation_id=None, question="q"))

    row = _row(Session, "r-err")
    assert row.status == "failed"
    assert "模型网关 502" in (row.error or "")


def test_超时标_failed_而不是挂起(Session, monkeypatch):
    async def hang(**_kw):
        await asyncio.sleep(10)
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", hang)
    monkeypatch.setattr(copilot_jobs, "RUN_TIMEOUT_S", 0.1)
    _mk_run(Session, "r-tmo")

    asyncio.run(copilot_jobs._execute("r-tmo", user_id="u1", conversation_id=None, question="q"))

    row = _row(Session, "r-tmo")
    assert row.status == "failed"
    assert "超时" in (row.error or "")


def test_终态不再被改写(Session, monkeypatch):
    """已经 ok 的 run 不许被后续一次执行写成 failed（状态机护栏）。"""
    async def boom(**_kw):
        raise RuntimeError("迟到的失败")

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", boom)
    _mk_run(Session, "r-done", status="ok")

    asyncio.run(copilot_jobs._execute("r-done", user_id="u1", conversation_id=None, question="q"))

    assert _row(Session, "r-done").status == "ok"


def test_调用期间_user_必须仍绑定_session(Session, monkeypatch):
    """生产事故回归（2026-10-25）：把脱离 session 的 User 传给 send_message 会
    DetachedInstanceError，run 直接 failed。这里钉住"调用时 user 仍 attached"。"""
    from sqlalchemy import inspect as sa_inspect

    from src.db.models import ChatConversation, User
    from src.web.api import chat as chat_api

    eng = Session.kw["bind"]
    for t in (User.__table__, ChatConversation.__table__):
        try:
            t.create(eng, checkfirst=True)
        except Exception as e:  # 某些 PG 专属列在 sqlite 建不出来就跳过本用例
            pytest.skip(f"sqlite 建表失败: {e}")

    db = Session()
    try:
        db.add(User(id="u-reg", username="reg", password_hash="x"))
        db.commit()
    finally:
        db.close()

    seen = {}

    async def fake_send(*, conversation_id, body, user):
        seen["attached"] = sa_inspect(user).session is not None
        seen["cid"] = conversation_id
        return None

    monkeypatch.setattr(chat_api, "send_message", fake_send)
    cid = asyncio.run(copilot_jobs.run_copilot_question(
        user_id="u-reg", conversation_id=None, question="问一句"))

    assert seen["attached"] is True, "user 必须在 send_message 期间仍绑定 session"
    assert cid == seen["cid"]


# ── 取消 / 越权 ───────────────────────────────────────────────────────────
def test_取消不在本进程的_run_按中断处理(client, Session):
    _mk_run(Session, "r-orphan", status="running")
    r = client.post("/api/copilot/runs/r-orphan/cancel")
    assert r.status_code == 200
    assert _row(Session, "r-orphan").status == "cancelled"


def test_已完成的不许取消(client, Session):
    _mk_run(Session, "r-fin", status="ok")
    assert client.post("/api/copilot/runs/r-fin/cancel").status_code == 409


def test_别人的_run_看不到也取消不了(client, Session):
    _mk_run(Session, "r-other", status="ok", user_id="u2")
    assert client.get("/api/copilot/runs/r-other/events").status_code == 404
    assert client.post("/api/copilot/runs/r-other/cancel").status_code == 404


# ── SSE ───────────────────────────────────────────────────────────────────
def test_SSE_推状态并在终态关闭(client, Session):
    _mk_run(Session, "r-sse", status="ok")
    text = client.get("/api/copilot/runs/r-sse/events").text
    assert "event: status" in text
    assert "event: done" in text
    assert '"status": "ok"' in text or '"status":"ok"' in text


def test_SSE_对不存在的_run_返回404(client):
    assert client.get("/api/copilot/runs/nope/events").status_code == 404
