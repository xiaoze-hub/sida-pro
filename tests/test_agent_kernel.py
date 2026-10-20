"""Agent 内核 P0：运行时（白名单/预算/计时）+ 执行轨迹（copilot_runs/copilot_steps）。

这一层的价值不在功能，在可归因：出了「竞价查不到」「认错标的」这类事故，
能直接查到那一轮调了哪些工具、参数是什么、返回了什么摘要、各花多久。
"""
from __future__ import annotations

import pytest

from src.db.models import CopilotRun, CopilotStep
from src.db.session import SessionLocal
from src.harness.runtime import BUDGET_EXCEEDED, AgentRuntime
from src.harness.trace import CopilotTrace


def _cleanup(run_id: str) -> None:
    db = SessionLocal()
    try:
        db.query(CopilotStep).filter(CopilotStep.run_id == run_id).delete()
        db.query(CopilotRun).filter(CopilotRun.run_id == run_id).delete()
        db.commit()
    finally:
        db.close()


def test_trace_records_run_and_steps():
    trace = CopilotTrace(conversation_id=42, user_id=1, recipe="auction_hunter", question="我的票竞价怎么样")
    trace.open()
    try:
        trace.step("get_auction_data", {"scene": "watchlist"}, result="600111 开盘 35.03 -0.57%", elapsed_ms=31)
        trace.step("get_watchlist", {}, result="10 只", elapsed_ms=8)
        trace.finish(status="ok")

        db = SessionLocal()
        try:
            run = db.query(CopilotRun).filter(CopilotRun.run_id == trace.run_id).one()
            assert run.status == "ok"
            assert run.tool_calls == 2
            assert run.recipe == "auction_hunter"
            assert run.elapsed_ms is not None and run.elapsed_ms >= 0
            steps = db.query(CopilotStep).filter(CopilotStep.run_id == trace.run_id).order_by(CopilotStep.seq).all()
            assert [s.tool for s in steps] == ["get_auction_data", "get_watchlist"]
            assert steps[0].args and "watchlist" in steps[0].args
            assert "35.03" in (steps[0].result_digest or "")
            assert steps[0].ok is True
        finally:
            db.close()
    finally:
        _cleanup(trace.run_id)


def test_trace_marks_error_status():
    trace = CopilotTrace(conversation_id=1, user_id=1, question="x")
    trace.open()
    try:
        trace.finish(status="error", error="LLMDegradedError: 429")
        db = SessionLocal()
        try:
            run = db.query(CopilotRun).filter(CopilotRun.run_id == trace.run_id).one()
            assert run.status == "error"
            assert "429" in (run.error or "")
        finally:
            db.close()
    finally:
        _cleanup(trace.run_id)


def test_digest_is_truncated_not_full_payload():
    """工具返回可能带用户持仓，落库只能是摘要。"""
    trace = CopilotTrace(conversation_id=1, user_id=1)
    trace.open()
    try:
        big = "持仓明细" * 400
        trace.step("get_portfolio", {}, result=big)
        db = SessionLocal()
        try:
            step = db.query(CopilotStep).filter(CopilotStep.run_id == trace.run_id).one()
            assert len(step.result_digest or "") < 600
        finally:
            db.close()
    finally:
        _cleanup(trace.run_id)


@pytest.mark.asyncio
async def test_budget_blocks_after_limit():
    rt = AgentRuntime(max_tool_calls=2)

    async def _ok(tool, args, **kw):
        return "ok"

    assert await rt.call("t1", {}, _ok) == "ok"
    assert await rt.call("t2", {}, _ok) == "ok"
    assert await rt.call("t3", {}, _ok) == BUDGET_EXCEEDED
    assert rt.calls == 2


@pytest.mark.asyncio
async def test_whitelist_rejects_and_records():
    trace = CopilotTrace(conversation_id=1, user_id=1)
    trace.open()
    rt = AgentRuntime(trace=trace, allowed_tools=frozenset({"get_quote"}))

    async def _exec(tool, args, **kw):
        return "unreachable"

    try:
        denied = await rt.call("order_stock", {}, _exec)
        assert "不在当前配方允许的范围内" in denied
        assert rt.denied == ["order_stock"]
        # 白名单内的工具照常执行
        assert await rt.call("get_quote", {"symbol": "600111"}, _exec) == "unreachable"
        assert rt.calls == 1
    finally:
        _cleanup(trace.run_id)


@pytest.mark.asyncio
async def test_runtime_passes_through_db_and_user_kwargs():
    seen = {}

    async def _exec(tool, args, **kw):
        seen.update({"tool": tool, "kw": kw})
        return "done"

    rt = AgentRuntime()
    out = await rt.call("get_quote", {"symbol": "600111"}, _exec, db="DB", user="U")
    assert out == "done"
    assert seen == {"tool": "get_quote", "kw": {"db": "DB", "user": "U"}}


def test_migration_registered():
    from src.web.migrations import MIGRATIONS

    ids = [m.version for m in MIGRATIONS]
    # 只钉"存在"与"连续"，不钉尾号（每次新增迁移都会红一次，2026-10-20 已踩两次）
    assert 208 in ids and 210 in ids
    assert max(ids) >= 211
    assert ids == list(range(ids[0], ids[-1] + 1))


# --- 集成：真实走一遍 _run_tool_loop（假 LLM + 假工具），验证轨迹端到端落库 ---

class _TC:
    def __init__(self, id_, name, args):
        self.id = id_
        self.function = type("F", (), {"name": name, "arguments": args})


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeClient:
    """第一轮要一个工具，第二轮直接给答案。"""

    def __init__(self):
        self.round = 0

    async def chat_with_tools(self, messages, tools=None, temperature=None):
        self.round += 1
        if self.round == 1:
            return _Msg(None, [_TC("c1", "get_quote", '{"symbol": "600111"}')])
        return _Msg("北方稀土 35.03 元，-0.57%。", None)

    async def chat_with_tools_stream(self, messages, tools=None, temperature=None):
        self.round += 1
        if self.round == 1:
            yield "msg", _Msg(None, [_TC("c1", "get_quote", '{"symbol": "600111"}')])
        else:
            yield "delta", "北方稀土 35.03 元。"
            yield "msg", _Msg("北方稀土 35.03 元。", None)


@pytest.mark.asyncio
async def test_tool_loop_writes_trace(monkeypatch):
    """验收线：跑一轮问答，copilot_runs 有 1 行 ok，copilot_steps 有对应工具。"""
    from src.web.api import chat as chat_mod

    async def _fake_exec(db, name, args, user=None):
        return f"{name}:{args.get('symbol')} 开盘 35.03"

    monkeypatch.setattr(chat_mod, "_execute_tool", _fake_exec)

    events = []
    async for kind, payload in chat_mod._run_tool_loop(
        _FakeClient(), [{"role": "user", "content": "600111 怎么样"}], db=None,
        user=None, conversation_id=777, question="600111 怎么样",
    ):
        events.append((kind, payload))

    assert ("text", "北方稀土 35.03 元，-0.57%。") in events

    db = SessionLocal()
    try:
        run = (db.query(CopilotRun)
               .filter(CopilotRun.conversation_id == 777)
               .order_by(CopilotRun.id.desc()).first())
        assert run is not None and run.status == "ok"
        assert run.tool_calls == 1
        steps = db.query(CopilotStep).filter(CopilotStep.run_id == run.run_id).all()
        assert len(steps) == 1
        assert steps[0].tool == "get_quote"
        assert "600111" in (steps[0].result_digest or "")
    finally:
        if run is not None:
            _cleanup(run.run_id)
        db.close()


def test_user_id_accepts_uuid_string():
    """回归：users.id 是 UUID 字符串，曾经 Declare 成 Integer 导致每条轨迹写入 DataError。"""
    trace = CopilotTrace(conversation_id=9, user_id="84425dba-9944-4f35-b804-fcdfc7ce6fa4", question="uuid")
    trace.open()
    try:
        trace.finish(status="ok")
        db = SessionLocal()
        try:
            run = db.query(CopilotRun).filter(CopilotRun.run_id == trace.run_id).one()
            assert run.user_id == "84425dba-9944-4f35-b804-fcdfc7ce6fa4"
        finally:
            db.close()
    finally:
        _cleanup(trace.run_id)
