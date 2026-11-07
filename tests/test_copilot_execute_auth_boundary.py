"""Execution authorization must not depend on schema visibility."""
import asyncio
from types import SimpleNamespace

from src.agents.chat.registry import CHAT_TOOL_REGISTRY, ChatTool
from src.web.api.chat import _execute_tool


def test_self_tool_rejects_missing_identity_before_handler(monkeypatch):
    called = []

    async def handler(db, args, user):
        called.append(True)
        return 'leaked'

    monkeypatch.setitem(CHAT_TOOL_REGISTRY, '_auth_test_self', ChatTool(
        name='_auth_test_self', schema=None, handler=handler, caliber='test', requires='self'
    ))
    result = asyncio.run(_execute_tool(None, '_auth_test_self', {}, None))
    assert not called
    assert 'leaked' not in result


def test_self_tool_with_identity_reaches_handler(monkeypatch):
    async def handler(db, args, user):
        return f'owner:{user.id}'

    monkeypatch.setitem(CHAT_TOOL_REGISTRY, '_auth_test_self', ChatTool(
        name='_auth_test_self', schema=None, handler=handler, caliber='test', requires='self'
    ))
    assert asyncio.run(_execute_tool(None, '_auth_test_self', {}, SimpleNamespace(id='u1'))) == 'owner:u1'
