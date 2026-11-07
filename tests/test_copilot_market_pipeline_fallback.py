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
