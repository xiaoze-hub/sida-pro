"""The actual chat loop, not a reconstruction of its dispatch order."""
from unittest.mock import AsyncMock

import pytest

from src.harness import institution_holdings, portfolio_risk, quick_answer
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
