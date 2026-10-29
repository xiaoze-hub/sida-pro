"""会话级 `initial_context` 的重放契约（2026-10-29 图表上下文「撤回」修正的后端事实源）。

前端把个股工作台那张图的读数拼进 `initial_context` 建会话。后端只有这一条写入路径：

    POST /chat/conversations            → chat_conversations.initial_context
    _build_ai_messages()                → 每一轮无条件把它拼进 system prompt
    _bind_conv_symbol()（换票）         → 只改 stock_symbol，不动 initial_context
    现有路由                            → 没有 PATCH/PUT，也没有任何接口能改/清这一列

于是「用户点清除」「改问另一只票」都不会让后端少注入一次旧快照。前端因此必须：

  - 首问之前清除：本地丢弃即可（服务端从未收到过这段文本）；
  - 首问之后清除：只能把后续追问引到一个**不带快照的新会话**（旧会话保留、不删除），
    并且在界面上明说旧会话仍带着它 —— 不许显示成"已清除"。

本文件钉住这条后端行为，防止前端再次假设"本地点一下就干净了"而留下假清除。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import Base, ChatConversation, ChatMessage

CHART_BLOCK = (
    "[图表上下文] 用户从个股工作台进入参谋时，屏幕上那张图的真实读数（进入时快照，不是实时值）。\n"
    "标的：600111（个股 · CN）\n"
    "周期：日线（来自页面 URL ?period=d1）\n"
    "可见区间：2026-06-01→2026-10-29，58 根，涨跌 +12.34%，振幅 23.45%"
)


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _conv(db, *, symbol="600111", market="SH", initial_context=None):
    conv = ChatConversation(
        user_id=None,
        title="契约会话",
        stock_symbol=symbol,
        stock_market=market,
        initial_context=initial_context,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _system_prompt(db, conv) -> str:
    """调用真实 `_build_ai_messages`（只把行情/技术取数挡在门外，避免联网）。"""
    import src.web.api.chat as chat_api

    async def _none(*_a, **_k):
        return ""

    orig_rt, orig_tc = chat_api._fetch_realtime_context, chat_api._fetch_technical_context
    chat_api._fetch_realtime_context = _none
    chat_api._fetch_technical_context = _none
    try:
        messages = asyncio.run(chat_api._build_ai_messages(db, conv, None))
    finally:
        chat_api._fetch_realtime_context = orig_rt
        chat_api._fetch_technical_context = orig_tc
    return messages[0]["content"]


def test_chart_snapshot_is_replayed_on_every_followup_turn(db):
    """同一会话的第二次、第三次追问，system prompt 里仍然有第一次建会话时那段快照。"""
    conv = _conv(db, initial_context=CHART_BLOCK)
    db.add(ChatMessage(conversation_id=conv.id, role="user", content="这个区间的量能怎么看"))
    db.add(ChatMessage(conversation_id=conv.id, role="assistant", content="（第一轮回答）"))
    db.commit()

    for turn in range(2, 4):
        db.add(ChatMessage(conversation_id=conv.id, role="user", content=f"第 {turn} 轮追问"))
        db.commit()
        system = _system_prompt(db, conv)
        assert "[图表上下文]" in system, "首轮快照在追问时没有被注入 → 后端契约变了，需重新评估前端"
        assert "2026-06-01" in system
        assert "进入时快照" in system
        assert "不是实时值" in system


def test_rebinding_symbol_keeps_the_old_chart_snapshot(db):
    """换票只改 stock_symbol；旧图的区间仍在 system prompt 里 → 前端不能复用该会话。"""
    from src.web.api.chat import SendMessageBody, _bind_conv_symbol

    conv = _conv(db, symbol="600111", market="SH", initial_context=CHART_BLOCK)
    db.add(ChatMessage(conversation_id=conv.id, role="user", content="先看看 600111"))
    db.commit()

    _bind_conv_symbol(db, conv, SendMessageBody(content="000776 怎么样", stock_symbol="000776"))
    db.refresh(conv)
    assert conv.stock_symbol == "000776"

    db.add(ChatMessage(conversation_id=conv.id, role="user", content="000776 怎么样"))
    db.commit()
    system = _system_prompt(db, conv)

    assert "000776" in system, "系统提示没有跟上新标的"
    assert "2026-06-01" in system, (
        "换票后旧图快照不见了 → 后端已支持撤回，前端应改用接口而不是新开会话"
    )
    assert "600111" in system
