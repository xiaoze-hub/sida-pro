"""A pipeline progress event is not a completed answer."""
import asyncio
from types import SimpleNamespace

from src.web.api import chat


def test_failed_streaming_pipeline_with_stage_falls_back(monkeypatch):
    recipe = SimpleNamespace(tools=[], label='市场测试', max_tool_calls=5,
                             pipeline=True, prompt_fragment='', output_sections=[])
    route = SimpleNamespace(recipe=recipe, tools=[], temperature=0.2)
    monkeypatch.setattr(chat, '_route_with_persona', lambda *args: (route, ''))
    monkeypatch.setattr(chat, '_inject_recipe', lambda *args: None)
    monkeypatch.setattr(chat, '_pipeline_ctx', lambda *args: '')
    monkeypatch.setattr(chat, 'is_stock_question', lambda *args: False)
    monkeypatch.setattr(chat, 'correction_note', lambda *args: '')

    async def pipeline(*args, **kwargs):
        yield 'stage', '正在采集'

    monkeypatch.setattr(chat, '_pipeline_answer_stream', pipeline)

    class FakeClient:
        async def chat_with_tools_stream(self, messages, tools, temperature):
            yield 'delta', '回退回答'

    async def run():
        return [event async for event in chat._run_tool_loop_inner(
            FakeClient(), [{'role': 'user', 'content': '市场情况'}], None, stream=True
        )]

    events = asyncio.run(run())
    assert ('stage', '正在采集') in events
    assert ('done', '回退回答') in events


def test_no_recipe_cannot_execute_tool_not_sent_to_model(monkeypatch):
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY, ChatTool

    calls = []

    async def hidden(db, args, user):
        calls.append(True)
        return 'should not run'

    monkeypatch.setitem(CHAT_TOOL_REGISTRY, '_hidden_tool', ChatTool(
        name='_hidden_tool', schema=None, handler=hidden, caliber='test', fallback=False
    ))
    route = SimpleNamespace(recipe=None, tools=[], temperature=0.2)
    monkeypatch.setattr(chat, '_route_with_persona', lambda *args: (route, ''))
    monkeypatch.setattr(chat, 'is_stock_question', lambda *args: False)
    monkeypatch.setattr(chat, 'correction_note', lambda *args: '')

    class FakeClient:
        def __init__(self):
            self.calls = 0

        async def chat_with_tools(self, messages, tools, temperature):
            self.calls += 1
            if self.calls == 1:
                call = SimpleNamespace(id='c1', function=SimpleNamespace(name='_hidden_tool', arguments='{}'))
                return SimpleNamespace(content=None, tool_calls=[call])
            return SimpleNamespace(content='完成', tool_calls=None)

    async def run():
        return [event async for event in chat._run_tool_loop_inner(
            FakeClient(), [{'role': 'user', 'content': '你好'}], None
        )]

    token = chat._REQ_TOOLS.set(None)
    try:
        asyncio.run(run())
    finally:
        chat._REQ_TOOLS.reset(token)
    assert not calls
