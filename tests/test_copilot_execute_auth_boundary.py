"""Execution authorization must not depend on schema visibility."""
import asyncio
from types import SimpleNamespace

from src.agents.chat.registry import CHAT_TOOL_REGISTRY, ChatTool
from src.web.api.auth import ServicePrincipal
from src.web.api.chat import _execute_tool, _REQ_BUDGET


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


def test_self_tools_reject_service_before_handler_and_budget(monkeypatch):
    called = []

    async def handler(db, args, user):
        called.append(True)
        return 'private'

    from dataclasses import replace
    for name in ('get_watchlist', 'get_notifications'):
        tool = CHAT_TOOL_REGISTRY[name]
        assert tool.requires == 'self'
        monkeypatch.setitem(CHAT_TOOL_REGISTRY, name, replace(tool, handler=handler))
        budget = {'used': 0, 'max': 5}
        token = _REQ_BUDGET.set(budget)
        try:
            result = asyncio.run(_execute_tool(None, name, {}, ServicePrincipal()))
            assert 'private' not in result
            assert not called
            assert budget['used'] == 0
        finally:
            _REQ_BUDGET.reset(token)
