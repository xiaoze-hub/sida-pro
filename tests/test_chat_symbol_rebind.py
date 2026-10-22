"""对话标的随页面切换重绑（2026-10-22「页面上下文串入」事故回归）。

事故：用户在 002548 页面聊过天，再到 002114 页面提问，参谋仍按 002548 作答。
根因：`src/web/api/chat.py::_bind_conv_symbol` 旧实现是"对话已绑定就直接返回"，
前端后来发来的 `stock_symbol` 被忽略 → 系统提示与取数上下文全串在旧票上。

修复口径（优先级不变，只是页面锚点从"永不更新"变成"可更新"）：
    文字里的 6 位代码 > @提及 > 页面锚点(body.stock_symbol) > 会话旧绑定

本文件只测判定层（`src/harness/context.py`）与绑定落库层（`_bind_conv_symbol`），
另有一条端到端用例验证"注入的系统提示 / 取数上下文"确实跟着换票。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.harness.context import (
    Mention,
    resolve_bound_symbol,
    sync_conv_symbol,
)
from src.web.api.chat import SendMessageBody, _bind_conv_symbol


class _DB:
    """最小 DB 替身：只记 commit/rollback 次数。"""

    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _conv(symbol=None, market=None):
    return SimpleNamespace(stock_symbol=symbol, stock_market=market)


def _body(content="x", symbol=None, market=None, mentions=None):
    return SendMessageBody(
        content=content,
        stock_symbol=symbol,
        stock_market=market,
        mentions=mentions or [],
    )


# ── 判定层：resolve_bound_symbol ────────────────────────────────────────────

def test_page_anchor_switches_binding():
    """页面从 002548 切到 002114 → 本轮锁定 002114。"""
    assert resolve_bound_symbol("002548", "002114", "这只怎么样") == "002114"


def test_same_page_anchor_keeps_old_binding():
    """页面没换（还是 002548）→ 保留旧绑定。"""
    assert resolve_bound_symbol("002548", "002548", "这只怎么样") == "002548"


def test_text_code_wins_over_page_anchor():
    """文字里写了 600111 → 文字优先于页面锚点（用户明说的永远最高）。"""
    assert resolve_bound_symbol("002548", "002114", "600111 怎么看") == "600111"


def test_mention_wins_over_page_anchor():
    """@提及优先于页面锚点（显式动作 > 隐含上下文）。"""
    body = _body(mentions=[Mention(type="stock", value="600111")])
    assert resolve_bound_symbol(
        "002548", "002114", "看看这个", mention="600111",
    ) == "600111"


def test_empty_page_anchor_keeps_old_binding():
    """页面没带锚点 → 保留旧绑定。"""
    assert resolve_bound_symbol("002548", None, "这只怎么样") == "002548"
    assert resolve_bound_symbol("002548", "", "这只怎么样") == "002548"


def test_invalid_page_anchor_keeps_old_binding():
    """非法锚点（非 6 位 / 带字母）→ 保留旧绑定，不报错。"""
    assert resolve_bound_symbol("002548", "北方稀土", "这只怎么样") == "002548"
    assert resolve_bound_symbol("002548", "abc123", "这只怎么样") == "002548"
    assert resolve_bound_symbol("002548", "12345", "这只怎么样") == "002548"
    assert resolve_bound_symbol("002548", "1234567", "这只怎么样") == "002548"


def test_no_binding_at_all_returns_none():
    """既没旧绑定也没锚点 → 不锁票。"""
    assert resolve_bound_symbol(None, None, "今天大盘怎么样") is None


# ── 落库层：_bind_conv_symbol ───────────────────────────────────────────────

def test_bind_conv_symbol_follows_page_switch():
    """旧绑定 002548 + 页面锚点 002114 → 绑定变成 002114。"""
    conv, db = _conv("002548", "SZ"), _DB()
    _bind_conv_symbol(db, conv, _body("这只怎么样", symbol="002114"))
    assert conv.stock_symbol == "002114"
    assert conv.stock_market == "SZ"
    assert db.commits == 1


def test_bind_conv_symbol_same_page_is_noop():
    """旧绑定 002548 + 页面锚点 002548 → 不变，且不额外 commit。"""
    conv, db = _conv("002548", "SZ"), _DB()
    _bind_conv_symbol(db, conv, _body("这只怎么样", symbol="002548"))
    assert (conv.stock_symbol, conv.stock_market) == ("002548", "SZ")
    assert db.commits == 0


def test_bind_conv_symbol_text_code_wins():
    """旧绑定 002548 + 页面锚点 002114 + 文字写 600111 → 以 600111 为准。"""
    conv, db = _conv("002548", "SZ"), _DB()
    _bind_conv_symbol(db, conv, _body("600111 怎么看", symbol="002114"))
    assert conv.stock_symbol == "600111"
    assert conv.stock_market == "SH"


def test_bind_conv_symbol_empty_anchor_keeps_old():
    """body 没带锚点 → 保留旧绑定。"""
    conv, db = _conv("002548", "SZ"), _DB()
    _bind_conv_symbol(db, conv, _body("这只怎么样"))
    assert (conv.stock_symbol, conv.stock_market) == ("002548", "SZ")
    assert db.commits == 0


def test_bind_conv_symbol_invalid_anchor_keeps_old():
    """body 锚点非法 → 保留旧绑定且不报错。"""
    conv, db = _conv("002548", "SZ"), _DB()
    _bind_conv_symbol(db, conv, _body("这只怎么样", symbol="北方稀土"))
    assert (conv.stock_symbol, conv.stock_market) == ("002548", "SZ")
    assert db.commits == 0


def test_bind_conv_symbol_market_follows_new_symbol():
    """换票时市场跟着换（002114 是深市 → SZ，不是沿用旧的 SH）。"""
    conv, db = _conv("600111", "SH"), _DB()
    _bind_conv_symbol(db, conv, _body("这只怎么样", symbol="002114"))
    assert (conv.stock_symbol, conv.stock_market) == ("002114", "SZ")


def test_bind_conv_symbol_uses_frontend_market_when_anchor_matches():
    """前端带来的市场在锚点就是这只票时优先（港美股只能靠它）。"""
    conv, db = _conv(None, None), _DB()
    _bind_conv_symbol(db, conv, _body("这只怎么样", symbol="00700", market="HK"))
    # 00700 不是 6 位 A 股码 → 非法锚点，保留旧绑定（这里是 None）
    assert conv.stock_symbol is None

    conv, db = _conv("600111", "SH"), _DB()
    _bind_conv_symbol(db, conv, _body("这只怎么样", symbol="600111", market="SH"))
    assert (conv.stock_symbol, conv.stock_market) == ("600111", "SH")


# ── @提及 与文字的交互（既有行为不能退化）──────────────────────────────────

def test_mention_binds_and_overrides_page_anchor():
    """@提及 002361 + 页面锚点 002114 → 以 @提及为准。"""
    conv, db = _conv("002548", "SZ"), _DB()
    body = _body(
        "看看这个",
        symbol="002114",
        mentions=[Mention(type="stock", value="002361")],
    )
    sync_conv_symbol(db, conv, body)
    assert conv.stock_symbol == "002361"
    assert conv.stock_market == "SZ"


def test_text_code_wins_over_mention():
    """文字里写了代码时以文字为准（系统提示按文字，工具锁定也按文字）。"""
    conv, db = _conv("002548", "SZ"), _DB()
    body = _body(
        "600111 怎么看",
        symbol="002114",
        mentions=[Mention(type="stock", value="002361")],
    )
    sync_conv_symbol(db, conv, body)
    assert conv.stock_symbol == "600111"


# ── 端到端：注入的系统提示 / 取数上下文必须跟着换票 ────────────────────────

def _conv_with_messages(db, symbol, market):
    """建一个带历史消息的会话（真实 ORM 行，走内存库）。"""
    from src.db.models import ChatConversation, ChatMessage

    conv = ChatConversation(
        user_id=None,
        title="回归会话",
        stock_symbol=symbol,
        stock_market=market,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    db.add(ChatMessage(conversation_id=conv.id, role="user", content="上一轮的问题"))
    db.commit()
    db.refresh(conv)
    return conv


def test_injected_system_prompt_and_context_follow_new_page():
    """旧绑定 002548 → 切到 002114 页面提问：
    注入的系统提示与取数上下文都必须是 002114，不能残留 002548。
    """
    from src.db.models import Base
    from src.web.api.chat import _build_ai_messages
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    try:
        conv = _conv_with_messages(db, "002548", "SZ")

        # 页面切换: 前端带来的锚点换成 002114
        body = _body("这只怎么样", symbol="002114")
        _bind_conv_symbol(db, conv, body)
        db.refresh(conv)

        assert conv.stock_symbol == "002114", "绑定没跟着页面切换"

        # 取数上下文按 002114 取 —— 打桩掉行情接口, 只看它被喂了哪只票
        seen: list[tuple[str, str]] = []

        async def _fake_realtime(symbol, market):
            seen.append(("realtime", symbol))
            return f"实时行情：测试（{market}:{symbol}）价格 10.00"

        async def _fake_technical(symbol, market):
            seen.append(("technical", symbol))
            return ""

        import src.web.api.chat as chat_api

        orig_rt, orig_tc = chat_api._fetch_realtime_context, chat_api._fetch_technical_context
        chat_api._fetch_realtime_context = _fake_realtime
        chat_api._fetch_technical_context = _fake_technical
        try:
            messages = asyncio.run(_build_ai_messages(db, conv, None))
        finally:
            chat_api._fetch_realtime_context = orig_rt
            chat_api._fetch_technical_context = orig_tc

        system = messages[0]["content"]
        assert "002114" in system, "系统提示里没有新标的"
        assert "002548" not in system, f"系统提示仍串着旧标的:\n{system[:800]}"
        assert seen and all(s == "002114" for _, s in seen), f"取数上下文仍在用旧票: {seen}"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_same_stock_followup_behavior_unchanged():
    """同一会话继续问同一只票 → 绑定与注入都不变（正常行为不退化）。"""
    from src.db.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    try:
        conv = _conv_with_messages(db, "002548", "SZ")
        body = _body("再看看它的资金流", symbol="002548")
        _bind_conv_symbol(db, conv, body)
        db.refresh(conv)
        assert (conv.stock_symbol, conv.stock_market) == ("002548", "SZ")
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
