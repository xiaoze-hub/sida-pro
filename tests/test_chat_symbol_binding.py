"""对话标的绑定（2026-10-19）：前端"正在看 XXX"必须绑到会话上。

事故背景：用户在 600111 工作台点「全景数智深度诊断」，请求体里没有标的，模型从持仓里
"猜"了用户主仓 002361（神剑股份），把两只票的分析混在一起；另一种表现是直接回
「没有股票标的」。修法 = 前端带 stock_symbol + 后端绑定到对话 + 提示词禁止替换标的。
"""
from __future__ import annotations

from types import SimpleNamespace

from src.web.api.chat import SendMessageBody, _bind_conv_symbol


class _DB:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _conv(symbol=None, market=None):
    return SimpleNamespace(stock_symbol=symbol, stock_market=market)


def test_binds_symbol_and_market():
    conv, db = _conv(), _DB()
    _bind_conv_symbol(db, conv, SendMessageBody(content="x", stock_symbol="600111"))
    assert (conv.stock_symbol, conv.stock_market) == ("600111", "SH")
    assert db.commits == 1


def test_market_inference_covers_sz_and_bj():
    for code, market in (("002361", "SZ"), ("300750", "SZ"), ("920001", "BJ"), ("430047", "BJ")):
        conv, db = _conv(), _DB()
        _bind_conv_symbol(db, conv, SendMessageBody(content="x", stock_symbol=code))
        assert conv.stock_market == market, code


def test_existing_binding_follows_page_switch():
    """2026-10-22 事故修订：旧实现"对话已绑定就不改"，页面锚点永远追不上。

    症状：用户在 002548 页面聊过天，再到 002114 页面提问，参谋仍按 002548 作答
    （系统提示与取数上下文全串在旧票上）。修复后页面锚点合法且与旧绑定不同 →
    绑定跟随更新；同页（锚点 == 旧绑定）才是"不变"。

    口径：文字里的 6 位代码 > @提及 > 页面锚点 > 会话旧绑定。
    """
    conv, db = _conv("600111", "SH"), _DB()
    _bind_conv_symbol(db, conv, SendMessageBody(content="x", stock_symbol="002361"))
    assert conv.stock_symbol == "002361"
    assert conv.stock_market == "SZ"
    assert db.commits == 1


def test_existing_binding_unchanged_when_page_is_the_same():
    """同一页面继续问 → 绑定不变（正常行为不退化）。"""
    conv, db = _conv("600111", "SH"), _DB()
    _bind_conv_symbol(db, conv, SendMessageBody(content="x", stock_symbol="600111"))
    assert (conv.stock_symbol, conv.stock_market) == ("600111", "SH")
    assert db.commits == 0


def test_invalid_code_is_ignored():
    conv, db = _conv(), _DB()
    _bind_conv_symbol(db, conv, SendMessageBody(content="x", stock_symbol="北方稀土"))
    assert conv.stock_symbol is None
    assert db.commits == 0


def test_missing_symbol_keeps_conversation_unbound():
    conv, db = _conv(), _DB()
    _bind_conv_symbol(db, conv, SendMessageBody(content="今天大盘怎么样"))
    assert conv.stock_symbol is None


# ── 工具层守卫（模型不许自己挑股票）────────────────────────────────────────────
import asyncio
from src.web.api import chat as chat_api


def test_dossier_refuses_when_nothing_should_be_analyzed():
    async def run():
        token = chat_api._REQ_BOUND.set(None)
        text = chat_api._REQ_TEXT.set("帮我全面分析一下")
        try:
            out = await chat_api._execute_tool(None, "get_stock_dossier", {"symbol": "600111"})
        finally:
            chat_api._REQ_BOUND.reset(token)
            chat_api._REQ_TEXT.reset(text)
        return out

    out = asyncio.run(run())
    assert "哪只股票" in out
    assert "600111" not in out


def test_dossier_forced_to_bound_symbol():
    async def run():
        token = chat_api._REQ_BOUND.set("600111")
        text = chat_api._REQ_TEXT.set("全面分析一下")
        seen = {}

        async def fake_handler(db, args, user):
            seen.update(args)
            return "ok"

        import dataclasses
        orig = chat_api.CHAT_TOOL_REGISTRY["get_stock_dossier"]
        chat_api.CHAT_TOOL_REGISTRY["get_stock_dossier"] = dataclasses.replace(orig, handler=fake_handler)
        try:
            await chat_api._execute_tool(None, "get_stock_dossier", {"symbol": "002361"})
        finally:
            chat_api.CHAT_TOOL_REGISTRY["get_stock_dossier"] = orig
            chat_api._REQ_BOUND.reset(token)
            chat_api._REQ_TEXT.reset(text)
        return seen

    assert asyncio.run(run())["symbol"] == "600111"


def test_dossier_allows_explicit_user_code():
    async def run():
        token = chat_api._REQ_BOUND.set("600111")
        text = chat_api._REQ_TEXT.set("002361 怎么样")
        seen = {}

        async def fake_handler(db, args, user):
            seen.update(args)
            return "ok"

        import dataclasses
        orig = chat_api.CHAT_TOOL_REGISTRY["get_stock_dossier"]
        chat_api.CHAT_TOOL_REGISTRY["get_stock_dossier"] = dataclasses.replace(orig, handler=fake_handler)
        try:
            await chat_api._execute_tool(None, "get_stock_dossier", {"symbol": "002361"})
        finally:
            chat_api.CHAT_TOOL_REGISTRY["get_stock_dossier"] = orig
            chat_api._REQ_BOUND.reset(token)
            chat_api._REQ_TEXT.reset(text)
        return seen

    assert asyncio.run(run())["symbol"] == "002361"
