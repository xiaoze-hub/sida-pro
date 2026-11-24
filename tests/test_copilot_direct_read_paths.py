"""The actual chat loop, not a reconstruction of its dispatch order."""
import asyncio
import sys
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from src import harness
from src.core.ai_client import AIClient
from src.db.models import User
from src.harness import direct_answer, institution_holdings, portfolio_risk, quick_answer
from src.harness.runtime import AgentRuntime
from src.harness.trace import CopilotTrace
from src.web.api import chat


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(("module", "question"), [
    (institution_holdings, "中央汇金最新报告期新进了哪些股票"),
    (portfolio_risk, "检查我的持仓风险，有哪些跌破防守位"),
    (quick_answer, "600111现价多少"),
])
async def test_chat_short_circuits_before_model(monkeypatch, stream, module, question):
    readers = {}
    for reader in (institution_holdings, portfolio_risk, quick_answer):
        readers[reader] = AsyncMock(return_value="确定性结果" if reader == module else None)
        monkeypatch.setattr(reader, "answer", readers[reader])
    db, user, runtime = object(), object(), object()

    def _unexpected(*args):
        pytest.fail("确定性直查未接入，错误进入模型路由")

    monkeypatch.setattr(chat, "_route_with_persona", _unexpected)
    messages = [{"role": "user", "content": question}]
    result = [event async for event in chat._run_tool_loop_inner(None, messages, db, user, stream=stream, runtime=runtime)]
    assert result == ([("delta", "确定性结果"), ("done", "确定性结果")] if stream else [("text", "确定性结果")])
    readers[module].assert_awaited_once_with(db, question, None, user, runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("forced", [frozenset(), frozenset({"get_watchlist"})])
async def test_explicit_tool_restriction_does_not_select_new_readers(monkeypatch, forced):
    class ReachedRouting(Exception):
        pass

    def _routing(*args):
        raise ReachedRouting

    monkeypatch.setattr(chat, "_route_with_persona", _routing)
    original = chat._REQ_TOOLS.set(forced)
    readers = (AsyncMock(return_value="should not run"), AsyncMock(return_value="should not run"))
    monkeypatch.setattr(portfolio_risk, "answer", readers[0])
    monkeypatch.setattr(institution_holdings, "answer", readers[1])
    try:
        with pytest.raises(ReachedRouting):
            async for _ in chat._run_tool_loop_inner(
                None, [{"role": "user", "content": "检查我的持仓风险，有哪些跌破防守位"}], None,
            ):
                pass
    finally:
        chat._REQ_TOOLS.reset(original)
    for reader in readers:
        reader.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["load", "call"])
async def test_optional_reader_failure_does_not_skip_next_reader(monkeypatch, caplog, failure):
    if failure == "load":
        monkeypatch.delattr(harness, "portfolio_risk")
        monkeypatch.setitem(sys.modules, portfolio_risk.__name__, None)
    else:
        monkeypatch.setattr(portfolio_risk, "answer", AsyncMock(side_effect=ValueError("private details")))
    other = AsyncMock(return_value="机构披露结果")
    monkeypatch.setattr(institution_holdings, "answer", other)
    trace = CopilotTrace()

    events = [event async for event in chat._run_tool_loop_inner(
        None, [{"role": "user", "content": "汇金最新报告期新进了哪些股票"}], None,
        runtime=AgentRuntime(trace=trace),
    )]
    assert events == [("text", "机构披露结果")]
    other.assert_awaited_once()
    assert trace.steps == []
    assert "portfolio_risk" in caplog.text and "private details" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_reader", [None, object(), lambda *_: "not an async reader"])
async def test_reader_configuration_failure_is_visible_when_no_reader_answers(monkeypatch, caplog, bad_reader):
    from src.harness import trace as trace_module

    db = SimpleNamespace(add=lambda _: None, commit=lambda: None, close=lambda: None)
    monkeypatch.setattr(trace_module, "SessionLocal", lambda: db)
    trace = trace_module.CopilotTrace()
    real_import = direct_answer.import_module

    def load(name):
        if name == institution_holdings.__name__:
            if bad_reader is None:
                raise ImportError("private deployment detail")
            return SimpleNamespace(answer=bad_reader)
        return real_import(name)

    monkeypatch.setattr(direct_answer, "import_module", load)
    result = await direct_answer.answer(
        None, "汇金持仓", None, None, AgentRuntime(trace=trace),
        stock_question=False, allow_specialized=True,
    )
    assert result is None
    assert trace.steps == [{"tool": "institution_holdings", "ok": False, "error": "reader_configuration_error"}]
    assert trace._infer_status("ok", None)[0] == "error"
    assert any(r.levelname == "ERROR" and "institution_holdings" in r.message for r in caplog.records)
    assert "private deployment detail" not in caplog.text


@pytest.mark.asyncio
async def test_unrecovered_reader_runtime_error_is_visible_in_run(monkeypatch, caplog):
    from src.harness import trace as trace_module

    db = SimpleNamespace(add=lambda _: None, commit=lambda: None, close=lambda: None)
    monkeypatch.setattr(trace_module, "SessionLocal", lambda: db)
    monkeypatch.setattr(institution_holdings, "answer", AsyncMock(side_effect=ValueError("private reader data")))
    trace = CopilotTrace()
    out = await direct_answer.answer(
        None, "汇金持仓", None, None, AgentRuntime(trace=trace), stock_question=False, allow_specialized=True,
    )
    assert out is None
    assert trace.steps == [{"tool": "institution_holdings", "ok": False, "error": "reader_execution_error"}]
    assert trace._infer_status("ok", None)[0] == "error"
    assert "private reader data" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_failed_portfolio_verification_stays_out_of_model_routing(monkeypatch, stream):
    def unavailable(*_):
        raise RuntimeError("holdings store unavailable")

    def unexpected_routing(*_):
        pytest.fail("a known verification failure must not be handed to the model")

    monkeypatch.setattr(portfolio_risk, "load_holdings", unavailable)
    monkeypatch.setattr(chat, "_route_with_persona", unexpected_routing)
    events = [event async for event in chat._run_tool_loop_inner(
        cast(AIClient, object()), [{"role": "user", "content": "我的持仓跌破防守位了吗"}], object(),
        cast(User, SimpleNamespace(id="u")), stream=stream,
    )]
    assert [kind for kind, _ in events] == (["delta", "done"] if stream else ["text"])
    assert all(isinstance(text, str) and "未完成核验，不能判断是否触及防守位" in text for _, text in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("stock_question", [False, True])
async def test_failed_readers_fall_through_to_main_model_loop(monkeypatch, stream, stock_question):
    for module in (quick_answer, portfolio_risk, institution_holdings):
        monkeypatch.setattr(module, "answer", AsyncMock(side_effect=RuntimeError("private details")))
    monkeypatch.setattr(chat, "_route_with_persona", lambda *_: (SimpleNamespace(recipe=None, tools=[], temperature=0), None))
    monkeypatch.setattr(chat, "is_stock_question", lambda _: stock_question)
    # No tools from the model on a stock question: keep the production zero-tool guard intact.
    raw = SimpleNamespace(content="模型正常返回", tool_calls=[])
    provider_calls = []

    async def stream_model(*args, **kwargs):
        provider_calls.append("stream")
        yield "delta", raw.content
        yield "done", raw

    ai = SimpleNamespace(chat_with_tools=AsyncMock(return_value=raw), chat_with_tools_stream=stream_model)
    token = chat._REQ_TOOLS.set(None)
    try:
        events = [event async for event in chat._run_tool_loop_inner(
            ai, [{"role": "user", "content": "检查持仓防守位"}], None, stream=stream,
        )]
    finally:
        chat._REQ_TOOLS.reset(token)
    final_text = chat.zero_tool_refusal() if stock_question else raw.content
    expected = [("done" if stream else "text", final_text)]
    if stream and not stock_question:
        expected.insert(0, ("delta", raw.content))
    assert events == expected
    if stream:
        assert provider_calls == ["stream"]
    else:
        ai.chat_with_tools.assert_awaited_once()


@pytest.mark.asyncio
async def test_reader_cancel_propagates_without_trying_other_readers(monkeypatch):
    monkeypatch.setattr(portfolio_risk, "answer", AsyncMock(side_effect=asyncio.CancelledError))
    other = AsyncMock(return_value="不能继续")
    monkeypatch.setattr(institution_holdings, "answer", other)
    with pytest.raises(asyncio.CancelledError):
        await direct_answer.answer(None, "汇金持仓", None, None, None, stock_question=False, allow_specialized=True)
    other.assert_not_awaited()
