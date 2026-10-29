"""Copilot 持仓上下文的模拟盘归属(2026-10-30 OCR security 回归)。

缺陷: `_build_portfolio_context` 把 `PaperTradingPosition.user_id IS NULL` 的
历史行当成"共享", 任何登录用户都能在 AI 提示里看到别人(或归属不明)的
模拟盘 股票/数量/入场价/止损。其余读路径(portfolio_diagnostics / user_data /
paper_trading)都只认 `user_id == uid`。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M
from src.web.api import chat as chat_api
from src.web.database import Base

MEMBER = cast(Any, SimpleNamespace(id="m1", role="member", username="m"))
OWNER = cast(Any, SimpleNamespace(id="o1", role="owner", username="o"))


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _paper(db, user_id, symbol="600519", qty=100, entry=10.0):
    db.add(
        M.PaperTradingPosition(
            user_id=user_id,
            stock_symbol=symbol,
            stock_market="CN",
            stock_name=symbol,
            quantity=qty,
            entry_price=entry,
            status="open",
        )
    )
    db.commit()


def test_member_cannot_see_null_owned_paper_rows(db):
    _paper(db, user_id=None)

    ctx = chat_api._build_portfolio_context(db, MEMBER)
    assert "600519" not in ctx, "NULL 归属的模拟盘不得注入 member 的提示"


def test_owner_keeps_visibility_of_legacy_null_rows(db):
    _paper(db, user_id=None)

    ctx = chat_api._build_portfolio_context(db, OWNER)
    assert "600519" in ctx, "owner 是数据责任人, 历史 NULL 行仍可见"


def test_member_sees_only_own_paper_rows(db):
    _paper(db, user_id="m1", symbol="000001", entry=8.0)
    _paper(db, user_id="other", symbol="600111", entry=9.0)
    _paper(db, user_id=None, symbol="300750", entry=7.0)

    ctx = chat_api._build_portfolio_context(db, MEMBER)
    assert "000001" in ctx
    assert "600111" not in ctx, "别人的模拟盘不得出现"
    assert "300750" not in ctx, "无主模拟盘不得出现"
