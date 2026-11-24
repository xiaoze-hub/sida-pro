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
import json
import logging
import time
from functools import partial

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
    copilot_jobs._ENQUEUED_AT.clear()
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


def test_后台运行启动即记录心跳(Session):
    _mk_run(Session, "r-heartbeat")
    copilot_jobs._mark("r-heartbeat", status="running")
    assert _row(Session, "r-heartbeat").heartbeat_at is not None


@pytest.mark.parametrize("terminal", ["cancelled", "failed"])
def test_并发终态先于收尾写入时不能被成功覆盖(Session, monkeypatch, terminal):
    from sqlalchemy import update
    from sqlalchemy.orm import Query

    _mk_run(Session, "r-race", status="running")
    original = Query.update
    raced = False

    def update_after_terminal(query, *args, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            db = Session()
            try:
                db.execute(update(CopilotRun).where(CopilotRun.run_id == "r-race").values(status=terminal))
                db.commit()
            finally:
                db.close()
        return original(query, *args, **kwargs)

    monkeypatch.setattr(Query, "update", update_after_terminal)
    copilot_jobs._mark("r-race", status="ok", conversation_id=99)
    assert _row(Session, "r-race").status == terminal


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_迟到轨迹不能重开或覆盖终态(Session, monkeypatch, status):
    from src.harness import trace as trace_mod

    monkeypatch.setattr(trace_mod, "SessionLocal", Session)
    _mk_run(Session, "r-terminal", status=status)
    trace = trace_mod.CopilotTrace(reuse_run_id="r-terminal")
    trace.open()
    trace.finish(status="ok")
    assert _row(Session, "r-terminal").status == status


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


def test_轨迹已写终态时仍要补上_conversation_id(Session, monkeypatch):
    """轨迹不知道前端会话 id，只有执行体知道；终态提前返回不能把它一起跳过。"""
    async def ok(**_kw):
        # 轨迹在已获得执行权的问答内部先落终态，随后问答才交还会话 id。
        assert copilot_jobs._mark("r-ok2", status="ok") is True
        return 99

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", ok)
    _mk_run(Session, "r-ok2")

    asyncio.run(copilot_jobs._execute("r-ok2", user_id="u1", conversation_id=None, question="q"))

    row = _row(Session, "r-ok2")
    assert row.conversation_id == 99
    assert row.status == "ok"


@pytest.mark.parametrize("status", ["degraded", "error"])
def test_轨迹降级或失败时补会话但不改终态(Session, status):
    _mk_run(Session, "r-partial", status=status)
    copilot_jobs._mark("r-partial", status="ok", conversation_id=99)
    row = _row(Session, "r-partial")
    assert row.status == status
    assert row.conversation_id == 99


def test_事件流不公开上游异常(client, Session):
    _mk_run(Session, "r-private", status="failed")
    db = Session()
    try:
        row = db.query(CopilotRun).filter_by(run_id="r-private").one()
        row.error = "gateway trace_id=secret"
        db.commit()
    finally:
        db.close()
    response = client.get("/api/copilot/runs/r-private/events")
    assert response.status_code == 200
    assert "secret" not in response.text
    assert "任务未能完成" in response.text


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


@pytest.mark.parametrize("terminal", ["failed", "error", "cancelled"])
def test_已是终态的_run_回真实状态而不是谎报取消(client, Session, terminal):
    """状态机不会再写，接口也不能回「已取消」：前端会拿它当刷新依据。"""
    _mk_run(Session, f"r-{terminal}", status=terminal)
    r = client.post(f"/api/copilot/runs/r-{terminal}/cancel")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == terminal
    assert body["cancelled"] is (terminal == "cancelled")
    assert _row(Session, f"r-{terminal}").status == terminal


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


# ── 阶段观测 / 内存账目 / 时钟 (2026-11-21 复审回归) ───────────────────────
def _stage_payloads(caplog, stage):
    out = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if "[copilot.stage]" not in msg:
            continue
        payload = json.loads(msg.split("] ", 1)[1])
        if payload.get("stage") == stage:
            out.append(payload)
    return out


@pytest.fixture()
def single_slot(monkeypatch):
    """把并发位压到 1 并重建信号量（`_slots()` 会缓存）。"""
    monkeypatch.setattr(copilot_jobs, "MAX_CONCURRENT", 1)
    monkeypatch.setattr(copilot_jobs, "_SLOTS", None)
    yield
    monkeypatch.setattr(copilot_jobs, "_SLOTS", None)


def test_dequeue_只在真正拿到并发位之后才记(Session, monkeypatch, caplog, single_slot):
    """「dequeue」必须是**真的开始跑**，不是「任务被调度起来了」。

    排队等 semaphore 的那段时间如果被记成 dequeue，排队等待就被前移成执行时间，
    阶段日志会把「没人干活」伪装成「干活很慢」。
    """
    async def quick(**_kw):
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", quick)
    _mk_run(Session, "r-queue")

    async def scenario():
        held = copilot_jobs._slots()
        await held.acquire()                     # 占满唯一的并发位
        copilot_jobs._ENQUEUED_AT["r-queue"] = time.monotonic()
        task = asyncio.create_task(copilot_jobs._execute(
            "r-queue", user_id="u1", conversation_id=None, question="q"))
        for _ in range(3):
            await asyncio.sleep(0)
        await asyncio.sleep(0.15)                # 仍在排队
        assert _row(Session, "r-queue").status == "queued"
        assert _stage_payloads(caplog, "dequeue") == [], "等并发位时不得记 dequeue"
        assert "r-queue" in copilot_jobs._ENQUEUED_AT, "排队账目必须保留到真正取到并发位"
        held.release()
        await task

    with caplog.at_level(logging.INFO, logger=copilot_jobs.logger.name):
        asyncio.run(scenario())

    payloads = _stage_payloads(caplog, "dequeue")
    assert len(payloads) == 1
    assert payloads[0]["run_id"] == "r-queue"
    assert payloads[0]["ms"] >= 100, f"排队 ~150ms 却被记成 {payloads[0]['ms']}ms"
    assert "r-queue" not in copilot_jobs._ENQUEUED_AT
    assert _row(Session, "r-queue").status == "ok"


@pytest.mark.parametrize("terminal", ["cancelled", "failed"])
def test_executor_does_not_start_after_terminal_state(Session, monkeypatch, terminal):
    executed = []

    async def unexpected(**_kwargs):
        executed.append(True)
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", unexpected)
    _mk_run(Session, "r-terminal", status=terminal)
    asyncio.run(copilot_jobs._execute("r-terminal", user_id="u1", conversation_id=None, question="q"))
    assert executed == [], "a terminal run must not start executing"
    assert _row(Session, "r-terminal").status == terminal


def test_cancel_before_first_schedule_persists_terminal_state(Session):
    async def scenario():
        submitted = await copilot_jobs.submit_run(
            copilot_jobs.SubmitRunBody(question="上证现在多少点"), user=_User(),
        )
        rid = submitted["run_id"]
        task = copilot_jobs._TASKS[rid]
        await copilot_jobs.cancel_run(rid, user=_User())
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _row(Session, rid).status == "cancelled"
        assert rid not in copilot_jobs._TASKS and rid not in copilot_jobs._ENQUEUED_AT

    asyncio.run(scenario())


def test_未启动即被取消也要清内存账目():
    """任务在第一次调度前就被 cancel 时，`_execute` 的 finally 根本不会跑。

    账目不清 = 每个「提交后立刻取消」的 run 都在进程里留一条永不释放的记录。
    """
    async def scenario():
        loop = asyncio.get_running_loop()
        # 真 Task：cancel 发生在第一次调度之前，协程体一行都不会跑
        wrapped = loop.create_task(asyncio.sleep(60))
        copilot_jobs._TASKS["r-never"] = wrapped
        copilot_jobs._ENQUEUED_AT["r-never"] = time.monotonic()
        wrapped.add_done_callback(partial(copilot_jobs._forget_task, "r-never"))

        wrapped.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wrapped

        assert "r-never" not in copilot_jobs._TASKS
        assert "r-never" not in copilot_jobs._ENQUEUED_AT

    asyncio.run(scenario())


def test_提交时先记入队时刻再建任务(client, Session, monkeypatch):
    """`_ENQUEUED_AT` 必须在 `create_task` **之前**写入。

    原实现先 create_task 再写账目：任务可能在写入之前就被调度，读到 None，
    于是这一轮永远没有 dequeue 观测值 —— 排队时间被静默丢掉。
    """
    seen = {}
    real_create_task = asyncio.create_task

    def spy_create_task(coro, **kw):
        seen.setdefault("at_create", set(copilot_jobs._ENQUEUED_AT))
        return real_create_task(coro, **kw)

    monkeypatch.setattr(copilot_jobs.asyncio, "create_task", spy_create_task)
    body = client.post("/api/copilot/runs", json={"question": "上证现在多少点"}).json()
    rid = body["run_id"]

    deadline = time.monotonic() + 5.0
    while rid in copilot_jobs._TASKS and time.monotonic() < deadline:
        time.sleep(0.01)                          # 等测试自己的后台任务收尾

    assert rid in seen.get("at_create", set()), "建任务时入队时刻必须已经写入"
    assert rid not in copilot_jobs._ENQUEUED_AT, "正常结束后账目必须清空"
    assert rid not in copilot_jobs._TASKS, "正常结束后任务句柄必须清空"


def test_耗时用单调钟而不是墙钟(Session, monkeypatch):
    """墙钟会跳（NTP 校时/夏令时）；耗时只能用 monotonic。"""
    async def quick(**_kw):
        return 7

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", quick)
    from datetime import datetime as _dt

    monkeypatch.setattr(copilot_jobs, "db_now_naive", lambda: _dt(2020, 1, 1))
    ticks = iter([100.0, 102.5])
    monkeypatch.setattr(copilot_jobs, "_monotonic", lambda: next(ticks))
    _mk_run(Session, "r-clock")

    asyncio.run(copilot_jobs._execute("r-clock", user_id="u1", conversation_id=None, question="q"))

    assert _row(Session, "r-clock").elapsed_ms == 2500
