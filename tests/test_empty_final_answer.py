"""空终稿兜底（2026-11-01 线上事故）。

现象：模型返回空内容，5 个工具全部成功、`copilot_runs.status=ok`，而
`chat_messages.content` 落库 0 字 —— 用户拿到一个空气泡，看板还算一次成功。

这里钉住三件事：① 空终稿必须换成能行动的话；② 本轮要发出 `degraded` 事件
（轨迹据此标 degraded，不再冒充成功）；③ 有正文时绝不触发兜底。
"""

from __future__ import annotations

import pytest

from src.core.answer_fallbacks import empty_final_notice
from src.web.api import chat as chat_api


class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _Call:
    def __init__(self, name: str, args: str = "{}"):
        self.id = f"call-{name}"
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": args})()


class _EmptyAfterTools:
    """第一轮调工具，第二轮返回空正文（线上就是这样）。"""

    def __init__(self):
        self.calls = 0

    async def chat_with_tools(self, messages, tools, temperature=0.5):
        self.calls += 1
        if self.calls == 1:
            return _Msg(tool_calls=[_Call("get_capital_flow", '{"symbol": "600111"}')])
        return _Msg(content="   ", tool_calls=None)

    async def chat_with_tools_stream(self, messages, tools, temperature=0.5):
        self.calls += 1
        if self.calls == 1:
            yield "tool_calls", _Msg(tool_calls=[_Call("get_capital_flow", '{"symbol": "600111"}')])
            return
        yield "delta", "  "

    async def chat_multi(self, messages, temperature=0.4):  # pragma: no cover
        return "x"


@pytest.fixture()
def _patched(monkeypatch):
    async def fake_execute(db, name, args, user=None):
        return "主力净流出 -6193万"

    monkeypatch.setattr(chat_api, "_execute_tool", fake_execute)
    return monkeypatch


async def _drain(client, stream: bool):
    out = []
    async for kind, payload in chat_api._run_tool_loop_inner(
        client, [{"role": "user", "content": "600111 主力资金"}], db=None, user=None, stream=stream,
    ):
        out.append((kind, payload))
    return out


@pytest.mark.asyncio
async def test_empty_final_is_replaced_and_marks_degraded(_patched):
    events = await _drain(_EmptyAfterTools(), stream=False)
    kinds = [k for k, _ in events]
    assert "degraded" in kinds, "空终稿必须发 degraded 事件，不能冒充成功"
    final = [p for k, p in events if k == "text"][-1]
    assert final.strip(), "空终稿不许原样返回空字符串"
    assert "没有返回内容" in final
    assert "重发" in final, "要给用户下一步动作"


@pytest.mark.asyncio
async def test_notice_counts_how_many_tools_ran(_patched):
    events = await _drain(_EmptyAfterTools(), stream=False)
    final = [p for k, p in events if k == "text"][-1]
    assert "1 项数据" in final, f"应说清已取到几项，实际: {final[:60]}"


def test_notice_without_tools_does_not_pretend_data():
    msg = empty_final_notice(0)
    # 不许声称"已取到 N 项"（根本一项都没跑）；结尾那句"已取到的数据不会重复消耗"是承诺，不算谎报
    assert "项数据" not in msg
    assert "没有返回内容" in msg
