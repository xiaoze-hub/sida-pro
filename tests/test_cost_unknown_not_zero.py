"""成本未知不得冒充 0(2026-10-30 OCR 复审 error 回归)。

缺陷: `/portfolio/summary` 用 `_safe_amount(pos.cost_price)` 把 NaN/NULL 成本变成 0,
于是 `pnl = 市值 - 0` 把**整笔市值**报成浮盈, `priced_cost` 少算, `cost_coverage_pct`
仍自称 100%。前端 `mergeQuoteIntoPortfolio` 里的 `safeNum(pos.cost_price) ?? 0` 同病。

本文件锁定:
  - 成本不可证(None / NaN / <=0)时: 该仓 pnl/pnl_pct = None, 成本不进任何分母,
    计数进 `cost_unknown_positions`, 但市值(行情事实)照算。
  - 账户/总计盈亏只在"有价 + 成本已知"子集上算(分子分母同篮子)。
  - `account_id=0` 按 0 过滤, 不得退化成"不过滤"。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M
from src.web.api import accounts as accounts_api
from src.web.database import Base

USER_A = cast(Any, SimpleNamespace(id="user-a", role="owner", username="a"))


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


def _stub_quotes(monkeypatch, quotes: dict):
    monkeypatch.setattr(accounts_api, "_fetch_quotes_for_stocks", lambda stocks: quotes)
    monkeypatch.setattr(accounts_api, "get_hkd_cny_rate", lambda: 0.92)
    monkeypatch.setattr(accounts_api, "get_usd_cny_rate", lambda: 7.25)


def _quote(price: float):
    return {
        "current_price": price,
        "prev_close": price,
        "change_pct": 0.0,
        "quote_time": "2026-10-30 10:00:00",
        "quote_date": "2026-10-30",
    }


def _account(db, name="A", funds=1000.0):
    acc = M.Account(name=name, available_funds=funds, enabled=True, user_id=USER_A.id)
    db.add(acc)
    db.flush()
    return acc


def _position(db, acc, symbol, cost, qty=100):
    st = M.Stock(symbol=symbol, name=symbol, market="CN")
    db.add(st)
    db.flush()
    db.add(
        M.Position(
            account_id=acc.id,
            stock_id=st.id,
            cost_price=cost,
            quantity=qty,
            user_id=None,
        )
    )
    db.commit()
    return st


def test_zero_cost_is_unknown_not_zero_profit(db, monkeypatch):
    """成本 0(脏值) + 有行情: 盈亏必须是 None, 不是"整笔市值都是赚的"。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0)})
    acc = _account(db)
    _position(db, acc, "600519", cost=0.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]
    pos = a["positions"][0]

    assert pos["cost_price"] is None
    assert pos["priced"] is True, "有现价就该算市值(行情事实)"
    assert pos["market_value_cny"] == pytest.approx(1200.0)
    assert pos["pnl"] is None, "成本未知不许给出盈亏(修复前是 1200 假浮盈)"
    assert pos["pnl_pct"] is None
    assert a["cost_unknown_positions"] == 1
    assert a["priced_cost"] == pytest.approx(0.0), "未知成本不得进成本分母"
    assert a["total_cost"] == pytest.approx(0.0)
    assert a["total_pnl"] is None, "没有可证成本就没有盈亏率"
    assert a["total_pnl_pct"] is None
    assert r["total"]["cost_unknown_positions"] == 1
    assert r["total"]["total_pnl"] is None


def test_negative_cost_is_unknown_not_zero_profit(db, monkeypatch):
    """负数成本(脏值)与 0/NULL 同口径, 不能变成 0。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0)})
    acc = _account(db)
    _position(db, acc, "600519", cost=-1.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    pos = r["accounts"][0]["positions"][0]
    assert pos["cost_price"] is None
    assert pos["pnl"] is None
    assert r["total"]["cost_unknown_positions"] == 1


def test_pnl_basket_only_counts_known_cost(db, monkeypatch):
    """混合: 只按"有价 + 成本已知"的仓算盈亏, 未知成本那条不进分母。"""
    _stub_quotes(
        monkeypatch,
        {"600519": _quote(12.0), "000001": _quote(20.0)},
    )
    acc = _account(db)
    _position(db, acc, "600519", cost=10.0, qty=100)  # 已知: 市值 1200 - 成本 1000 = 200
    _position(db, acc, "000001", cost=-1.0, qty=100)  # 脏成本: 市值 2000, 盈亏不给

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]

    assert a["total_market_value"] == pytest.approx(3200.0)
    assert a["priced_cost"] == pytest.approx(1000.0)
    assert a["total_cost"] == pytest.approx(1000.0)
    assert a["cost_unknown_positions"] == 1
    assert a["total_pnl"] == pytest.approx(200.0), "盈亏只能来自已知成本那 1000 元篮子"
    assert a["total_pnl_pct"] == pytest.approx(20.0)
    assert r["total"]["total_pnl"] == pytest.approx(200.0)


def test_account_id_zero_filters_instead_of_listing_everything(db, monkeypatch):
    """`?account_id=0` 必须按 0 过滤(空集), 不能静默返回全部账户。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0)})
    acc = _account(db)
    _position(db, acc, "600519", cost=10.0)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A, account_id=0)
    assert r["accounts"] == [], "account_id=0 不得退化成不过滤"
    assert r["total"]["total_market_value"] == 0
