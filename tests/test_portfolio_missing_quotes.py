"""持仓缺行情时禁止伪造收益/市值(2026-10-29 缺陷回归)。

缺陷(审计发现):
  1. `/portfolio/summary`: 成本无条件全额累加, 市值只在有现价时累加 →
     全持仓缺行情时 total_pnl = -全部成本, total_pnl_pct = -100%(伪造亏损)。
  2. `/portfolio/diagnostics`(`_gather_holdings`): 缺现价时 `mv = 成本`,
     浮盈记 0 —— 用成本冒充市值, 集中度/市值分布全部失真。

修复契约(本文件锁定):
  - 现价不可用(缺行情 / 0 / 负数 / NaN)时: market_value、pnl、market_value_cny 一律 None,
    绝不用成本代替、绝不记 0。
  - 汇总口径: 市值与盈亏只在**已定价子集**上计算(分子分母同篮子),
    priced_cost 与总成本分开暴露; 全缺行情时 total_pnl/total_pnl_pct/total_market_value 为 None。
  - 覆盖度: 每个账户与汇总都带 quotes_coverage(持仓总数/已定价/缺失/成本覆盖率)。
  - 行情齐全时(旧可用数据路径)行为与修复前完全一致。
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

# 直接调用 API 函数时用 fake user(与 tests/test_portfolio_result_cache.py 同样的做法)
USER_A = cast(Any, SimpleNamespace(id="user-a", role="owner", username="a"))
USER_B = cast(Any, SimpleNamespace(id="user-b", role="owner", username="b"))


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
    """固定行情 + 固定汇率, 测试完全离线。"""
    monkeypatch.setattr(accounts_api, "_fetch_quotes_for_stocks", lambda stocks: quotes)
    monkeypatch.setattr(accounts_api, "get_hkd_cny_rate", lambda: 0.92)
    monkeypatch.setattr(accounts_api, "get_usd_cny_rate", lambda: 7.25)


def _quote(price, prev_close=None, change_pct=None):
    return {
        "current_price": price,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "quote_time": "2026-10-29 10:00:00",
        "quote_date": "2026-10-29",
    }


def _account(db, user_id=USER_A.id, name="A", funds=1000.0, enabled=True):
    acc = M.Account(name=name, available_funds=funds, enabled=enabled, user_id=user_id)
    db.add(acc)
    db.flush()
    return acc


def _position(db, acc, symbol, cost=10.0, qty=100, market="CN"):
    st = M.Stock(symbol=symbol, name=symbol, market=market)
    db.add(st)
    db.flush()
    db.add(M.Position(account_id=acc.id, stock_id=st.id, cost_price=cost, quantity=qty))
    db.commit()
    return st


# ---------------- summary: 全缺行情 ----------------


def test_summary_all_missing_quotes_never_fabricates_loss(db, monkeypatch):
    """全持仓缺行情: 不得出 -100% / 不得把成本当市值。"""
    _stub_quotes(monkeypatch, {})
    acc = _account(db)
    _position(db, acc, "600519", cost=10.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]

    assert a["total_pnl"] is None, "全缺行情不得给出盈亏(修复前是 -1000 假亏损)"
    assert a["total_pnl_pct"] is None, "全缺行情不得给出盈亏率(修复前是 -100%)"
    assert a["total_market_value"] is None, "无任何有效现价时不得用 0/成本冒充市值"
    assert a["total_cost"] == pytest.approx(1000.0), "成本是已知事实, 仍应如实返回"
    assert a["total_assets"] is None, "市值未知时总资产无法确定, 不得用成本凑"
    assert a["quotes_coverage"]["positions_total"] == 1
    assert a["quotes_coverage"]["positions_priced"] == 0
    assert a["quotes_coverage"]["positions_missing"] == 1
    assert a["quotes_coverage"]["complete"] is False

    pos = a["positions"][0]
    assert pos["quote_status"] == "missing"
    assert pos["market_value"] is None
    assert pos["market_value_cny"] is None
    assert pos["pnl"] is None
    assert pos["pnl_pct"] is None
    assert pos["current_price"] is None

    total = r["total"]
    assert total["total_pnl"] is None
    assert total["total_pnl_pct"] is None
    assert total["total_market_value"] is None
    assert total["quotes_coverage"] == a["quotes_coverage"]


def test_summary_zero_price_is_not_a_valid_mark(db, monkeypatch):
    """行情源给 0 价(停牌/坏数据): 记为无效报价, 不当作 0 市值 → 不得出 -100%。"""
    _stub_quotes(monkeypatch, {"600519": _quote(0.0)})
    acc = _account(db)
    _position(db, acc, "600519", cost=10.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]
    pos = a["positions"][0]

    assert pos["quote_status"] == "invalid_price"
    assert pos["current_price"] is None, "0 价不可折算市值"
    assert pos["raw_current_price"] == 0.0, "原始值保留供排查"
    assert pos["market_value"] is None and pos["pnl"] is None
    assert a["total_pnl"] is None and a["total_pnl_pct"] is None


# ---------------- summary: 部分缺价 ----------------


def test_summary_partial_quotes_uses_priced_basket_only(db, monkeypatch):
    """部分缺行情: 盈亏只在已定价子集上算(分子分母同篮子), 并暴露覆盖度。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0, prev_close=11.0, change_pct=9.09)})
    acc = _account(db, funds=1000.0)
    _position(db, acc, "600519", cost=10.0, qty=100)  # 已定价: 成本 1000, 市值 1200
    _position(db, acc, "000001", cost=5.0, qty=100)   # 缺行情: 成本 500, 市值未知

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]

    assert a["total_cost"] == pytest.approx(1500.0), "总成本仍是全持仓成本"
    assert a["priced_cost"] == pytest.approx(1000.0)
    assert a["unpriced_cost"] == pytest.approx(500.0)
    assert a["total_market_value"] == pytest.approx(1200.0), "只含已定价持仓"
    assert a["total_pnl"] == pytest.approx(200.0), "修复前会把缺价仓位算成 -500 亏损"
    assert a["total_pnl_pct"] == pytest.approx(20.0), "分母同为已定价成本, 不是 200/1500"
    assert a["total_assets"] == pytest.approx(1200.0 + 1000.0)

    cov = a["quotes_coverage"]
    assert cov["positions_total"] == 2
    assert cov["positions_priced"] == 1
    assert cov["positions_missing"] == 1
    assert cov["cost_coverage_pct"] == pytest.approx(66.67, abs=0.01)
    assert cov["complete"] is False

    by_symbol = {p["symbol"]: p for p in a["positions"]}
    assert by_symbol["600519"]["pnl"] == pytest.approx(200.0)
    assert by_symbol["600519"]["quote_status"] == "ok"
    assert by_symbol["000001"]["market_value"] is None
    assert by_symbol["000001"]["pnl"] is None
    assert by_symbol["000001"]["quote_status"] == "missing"

    total = r["total"]
    assert total["total_market_value"] == pytest.approx(1200.0)
    assert total["total_pnl"] == pytest.approx(200.0)
    assert total["total_pnl_pct"] == pytest.approx(20.0)
    assert total["quotes_coverage"]["positions_missing"] == 1


# ---------------- summary: 旧有可用行情路径不变 ----------------


def test_summary_full_quotes_behaviour_unchanged(db, monkeypatch):
    """行情齐全: 与修复前逐字段一致(防止修缺价把正常路径改坏)。"""
    _stub_quotes(
        monkeypatch,
        {
            "600519": _quote(12.0, prev_close=11.0, change_pct=9.09),
            "000001": _quote(4.0, prev_close=4.0, change_pct=0.0),
        },
    )
    acc = _account(db, funds=500.0)
    _position(db, acc, "600519", cost=10.0, qty=100)
    _position(db, acc, "000001", cost=5.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]

    assert a["total_market_value"] == pytest.approx(1600.0)
    assert a["total_cost"] == pytest.approx(1500.0)
    assert a["total_pnl"] == pytest.approx(100.0)
    assert a["total_pnl_pct"] == pytest.approx(6.67, abs=0.01)
    assert a["total_assets"] == pytest.approx(2100.0)
    assert a["total_daily_pnl"] == pytest.approx(100.0)  # (12-11)*100
    assert a["quotes_coverage"]["complete"] is True
    assert a["quotes_coverage"]["cost_coverage_pct"] == pytest.approx(100.0)
    assert a["positions"][0]["quote_status"] == "ok"
    assert r["total"]["total_pnl_pct"] == pytest.approx(6.67, abs=0.01)


def test_summary_include_quotes_false_keeps_legacy_numeric_zero_mode(db, monkeypatch):
    """显式 include_quotes=false 是「不计价」模式(前端按本地行情重算), 保持旧数值 0 语义,
    但必须标注 requested=false, 不能与「缺行情」混为一谈。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0)})
    acc = _account(db, funds=1000.0)
    _position(db, acc, "600519", cost=10.0, qty=100)

    r = accounts_api.get_portfolio_summary(include_quotes=False, db=db, user=USER_A)
    a = r["accounts"][0]

    assert a["total_market_value"] == 0
    assert a["total_pnl"] == 0
    assert a["total_pnl_pct"] == 0
    assert a["total_assets"] == pytest.approx(1000.0)
    assert a["quotes_coverage"]["requested"] is False
    assert a["positions"][0]["quote_status"] == "not_requested"


# ---------------- diagnostics: 缺价不得用成本冒充市值 ----------------


def test_diagnostics_gather_holdings_never_uses_cost_as_market_value(db, monkeypatch):
    """`_gather_holdings` 缺现价时必须置 None, 不得回填成本(修复前 mv=成本, pnl=0)。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0)})
    acc = _account(db)
    _position(db, acc, "600519", cost=10.0, qty=100)
    _position(db, acc, "000001", cost=5.0, qty=100)

    holdings = accounts_api._gather_holdings(db, USER_A)
    by_symbol = {h["symbol"]: h for h in holdings}

    assert by_symbol["600519"]["market_value"] == pytest.approx(1200.0)
    assert by_symbol["600519"]["unrealized_pnl"] == pytest.approx(200.0)
    assert by_symbol["000001"]["market_value"] is None, "缺价不得用成本 500 冒充市值"
    assert by_symbol["000001"]["unrealized_pnl"] is None, "缺价不得记 0 浮盈"
    assert by_symbol["000001"]["quote_status"] == "missing"


def test_diagnostics_partial_coverage_reported(db, monkeypatch):
    """诊断结果只能统计已定价部分, 并明确缺失数与覆盖度。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0)})
    acc = _account(db)
    _position(db, acc, "600519", cost=10.0, qty=100)
    _position(db, acc, "000001", cost=5.0, qty=100)

    diag = accounts_api.portfolio_diagnostics(db=db, user=USER_A)

    assert diag["position_count"] == 2
    assert diag["priced_position_count"] == 1
    assert diag["missing_quote_count"] == 1
    assert diag["total_market_value"] == pytest.approx(1200.0), "缺价仓位不得计入(更不得按成本计入)"
    assert diag["total_unrealized_pnl"] == pytest.approx(200.0)
    assert diag["quotes_coverage_pct"] == pytest.approx(50.0)
    assert diag["has_complete_quotes"] is False
    assert any("缺" in a and "行情" in a for a in diag["alerts"])
    assert diag["max_weight"] == pytest.approx(1.0)


def test_diagnostics_all_priced_unchanged(db, monkeypatch):
    """行情齐全: 诊断数值与修复前一致。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0), "000001": _quote(4.0)})
    acc = _account(db)
    _position(db, acc, "600519", cost=10.0, qty=100)
    _position(db, acc, "000001", cost=5.0, qty=100)

    diag = accounts_api.portfolio_diagnostics(db=db, user=USER_A)

    assert diag["total_market_value"] == pytest.approx(1600.0)
    assert diag["total_unrealized_pnl"] == pytest.approx(100.0)
    assert diag["by_market"]["CN"] == pytest.approx(1600.0)
    assert diag["missing_quote_count"] == 0
    assert diag["has_complete_quotes"] is True


def test_summary_empty_account_is_zero_not_unknown(db, monkeypatch):
    """空账户(无持仓)的 0 盈亏是真实的 0, 不能被"缺行情未知"吞掉。"""
    _stub_quotes(monkeypatch, {})
    _account(db, funds=800.0)  # 没有任何持仓

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]

    assert a["total_market_value"] == 0
    assert a["total_pnl"] == 0
    assert a["total_pnl_pct"] == 0
    assert a["total_assets"] == pytest.approx(800.0)
    assert a["quotes_coverage"]["positions_total"] == 0
    assert r["total"]["total_pnl"] == 0


# ---------------- 多账号隔离 ----------------


def test_missing_quotes_do_not_leak_other_users(db, monkeypatch):
    """缺行情改造不得放宽归属过滤: A 的汇总/诊断看不到 B 的账户。"""
    _stub_quotes(monkeypatch, {})
    acc_a = _account(db, user_id=USER_A.id, name="A")
    _position(db, acc_a, "600519", cost=10.0, qty=100)
    acc_b = _account(db, user_id=USER_B.id, name="B")
    _position(db, acc_b, "000001", cost=5.0, qty=1000)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    assert [a["name"] for a in r["accounts"]] == ["A"]
    assert r["total"]["total_cost"] == pytest.approx(1000.0), "B 的成本不得计入 A 的汇总"

    diag = accounts_api.portfolio_diagnostics(db=db, user=USER_A)
    assert diag["position_count"] == 1


# ---------------- 模拟盘诊断: 同样不得用成本/入场价冒充现价 ----------------


def test_paper_diagnostics_missing_price_not_valued_at_entry(monkeypatch):
    """模拟盘诊断: current_price 缺失时不得用 entry_price 当现价估值。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine,
        tables=[M.PaperTradingPosition.__table__],
    )
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    import src.core.portfolio_diagnostics as diag_mod

    monkeypatch.setattr(diag_mod, "SessionLocal", TestingSession)

    s = TestingSession()
    s.add(
        M.PaperTradingPosition(
            user_id=USER_A.id,
            stock_symbol="600519",
            stock_market="CN",
            quantity=100,
            entry_price=100.0,
            current_price=None,
            unrealized_pnl=0.0,
            status="open",
        )
    )
    s.commit()
    s.close()

    diag = diag_mod.diagnose_paper_portfolio(user_id=USER_A.id)
    assert diag["position_count"] == 1
    assert diag["missing_quote_count"] == 1
    assert diag["total_market_value"] != pytest.approx(100.0 * 100), "不得用入场价 100 冒充现价"
    assert diag["has_complete_quotes"] is False
