"""一次执行只能有一条轨迹（2026-10-25 生产事故回归）。

事故现象：异步提交 `POST /copilot/runs` 后，库里出现**两条**记录
```
5651652c ok tools=0 steps=0 conv=321 07:51:29.438   ← 提交时建的（空壳）
0999d6f2 ok tools=1 steps=1 conv=321 07:51:29.598   ← _run_tool_loop 又开了一条
```
差 160ms 同一会话。`ok_rate` / `tool_calls` / 成本统计全部重复计数 —— 而轨迹正是
自改造(RST)的度量基础，脏数据会让任何"改配方→看指标"的对照失真。

修法：执行体把自己的 run_id 通过 contextvar 交给 `start_copilot_run` **复用**，
不新建。
"""

from __future__ import annotations

from contextvars import ContextVar

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from src.db.models import CopilotRun, CopilotStep
from src.harness import trace as trace_mod


@pytest.fixture()
def Session(monkeypatch):
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    CopilotRun.__table__.create(eng)
    CopilotStep.__table__.create(eng)
    factory = sessionmaker(bind=eng)

    import src.db.session as session_mod
    monkeypatch.setattr(session_mod, "SessionLocal", factory)
    # trace.py 在模块级 `from src.db.session import SessionLocal`，改源模块对它无效，
    # 必须直接改它自己那份绑定（否则会写到真实库，测试读的是内存库 → 断言 queued）
    monkeypatch.setattr(trace_mod, "SessionLocal", factory)
    return factory


def _seeded(Session, run_id="r-ext", status="queued"):
    db = Session()
    try:
        db.add(CopilotRun(run_id=run_id, user_id="u1", status=status,
                          question="q", tool_calls=0))
        db.commit()
    finally:
        db.close()


def _runs(Session):
    db = Session()
    try:
        return db.query(CopilotRun).all()
    finally:
        db.close()


def test_给了外部_run_id_就复用不新建(Session):
    _seeded(Session, "r-ext")
    token = trace_mod.EXTERNAL_RUN_ID.set("r-ext")
    try:
        tr = trace_mod.start_copilot_run(conversation_id=9, user_id="u1", question="q")
        assert tr is not None and tr.run_id == "r-ext"
        tr.step("get_range_summary", {"symbol": "000001"}, ok=True, elapsed_ms=12)
        trace_mod.finish_copilot_run(tr, status="ok")
    finally:
        trace_mod.EXTERNAL_RUN_ID.reset(token)

    rows = _runs(Session)
    assert len(rows) == 1, f"只能有一条 run，实际 {len(rows)} 条（重复计数）"
    row = rows[0]
    assert row.run_id == "r-ext"
    assert row.status == "ok"
    assert row.tool_calls == 1, "步骤要落到同一条 run 上"
    assert row.finished_at is not None


def test_没有外部_run_id_时行为不变_自己开一条(Session):
    token = trace_mod.EXTERNAL_RUN_ID.set(None)   # ContextVar 的属性是只读的，只能 set
    try:
        tr = trace_mod.start_copilot_run(conversation_id=9, user_id="u1", question="q")
    finally:
        trace_mod.EXTERNAL_RUN_ID.reset(token)

    assert tr is not None
    tr.step("search_news", {"days": 7}, ok=True, elapsed_ms=30)
    trace_mod.finish_copilot_run(tr, status="ok")

    rows = _runs(Session)
    assert len(rows) == 1
    assert rows[0].run_id == tr.run_id
    assert rows[0].tool_calls == 1


def test_外部_run_行不存在时退回自己插入_不把执行搞挂(Session):
    """run 行被清掉（回滚/清库）时不能抛，否则用户的问题会跟着失败。"""
    assert _runs(Session) == []
    token = trace_mod.EXTERNAL_RUN_ID.set("ghost-run")
    try:
        tr = trace_mod.start_copilot_run(conversation_id=9, user_id="u1", question="q")
        assert tr is not None
        trace_mod.finish_copilot_run(tr, status="ok")
    finally:
        trace_mod.EXTERNAL_RUN_ID.reset(token)

    rows = _runs(Session)
    assert len(rows) == 1 and rows[0].run_id == "ghost-run"


def test_复用时不覆盖外部已写的_conversation_id(Session):
    _seeded(Session, "r-2")
    token = trace_mod.EXTERNAL_RUN_ID.set("r-2")
    try:
        tr = trace_mod.start_copilot_run(conversation_id=None, user_id="u1", question="q")
        trace_mod.finish_copilot_run(tr, status="ok")
    finally:
        trace_mod.EXTERNAL_RUN_ID.reset(token)

    db = Session()
    try:
        row = db.query(CopilotRun).filter(CopilotRun.run_id == "r-2").one()
        assert row.status == "ok"
    finally:
        db.close()


def test_默认_contextvar_为空():
    assert trace_mod.EXTERNAL_RUN_ID.get() is None
    assert isinstance(trace_mod.EXTERNAL_RUN_ID, ContextVar)
