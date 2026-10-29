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


def _position(db, acc, symbol, cost=10.0, qty=100, market="CN", user_id=None):
    st = M.Stock(symbol=symbol, name=symbol, market=market)
    db.add(st)
    db.flush()
    db.add(
        M.Position(
            account_id=acc.id,
            stock_id=st.id,
            cost_price=cost,
            quantity=qty,
            user_id=user_id,
        )
    )
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
    assert a["total_assets"] is None, "子集市值 + 全部资金不是总资产"
    assert a["total_assets_complete"] is False
    assert a["total_assets_priced_subset"] == pytest.approx(1200.0 + 1000.0)

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


# ================= 复审追加: 部分覆盖 / 坏值 / 归属 / 汇率时效 =================


def test_summary_all_missing_quotes_daily_pnl_is_unknown(db, monkeypatch):
    """全缺行情: 当日盈亏也是未知, 不得用 0 冒充「今天没涨没跌」。"""
    _stub_quotes(monkeypatch, {})
    acc = _account(db)
    _position(db, acc, "600519", cost=10.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]

    assert a["total_daily_pnl"] is None
    cov = a["daily_pnl_coverage"]
    assert cov["requested"] is True
    assert cov["positions_total"] == 1
    assert cov["positions_observed"] == 0
    assert cov["complete"] is False
    assert r["total"]["total_daily_pnl"] is None
    assert r["total"]["daily_pnl_coverage"]["complete"] is False


def test_daily_pnl_without_prev_close_is_unknown_not_zero(db, monkeypatch):
    """有现价但缺昨日收盘: 当日盈亏未知(不得记 0)。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0)})
    acc = _account(db)
    _position(db, acc, "600519", cost=10.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]
    pos = a["positions"][0]

    assert pos["current_price"] == pytest.approx(12.0)
    assert pos["daily_pnl"] is None and pos["daily_pnl_pct"] is None
    assert a["total_daily_pnl"] is None
    assert a["daily_pnl_coverage"]["positions_observed"] == 0
    assert a["daily_pnl_coverage"]["complete"] is False


def test_daily_pnl_partial_prev_close_is_flagged_incomplete(db, monkeypatch):
    """部分持仓有昨日收盘: 汇总只能算观测到的部分, 必须标注覆盖不完整。"""
    _stub_quotes(
        monkeypatch,
        {
            "600519": _quote(12.0, prev_close=11.0),
            "000001": _quote(4.0),  # 缺 prev_close
        },
    )
    acc = _account(db)
    _position(db, acc, "600519", cost=10.0, qty=100)
    _position(db, acc, "000001", cost=5.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]

    assert a["total_daily_pnl"] == pytest.approx(100.0)
    cov = a["daily_pnl_coverage"]
    assert cov["positions_total"] == 2
    assert cov["positions_observed"] == 1
    assert cov["complete"] is False


def test_summary_partial_quotes_total_assets_marked_incomplete(db, monkeypatch):
    """部分缺行情: 已定价市值 + 全部可用资金不是总资产, 不得当作完整总资产返回。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0)})
    acc = _account(db, funds=1000.0)
    _position(db, acc, "600519", cost=10.0, qty=100)
    _position(db, acc, "000001", cost=5.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]

    assert a["total_assets"] is None, "子集市值+资金不是总资产"
    assert a["total_assets_complete"] is False
    assert a["total_assets_basis"] == "priced_positions_only"
    assert a["total_assets_priced_subset"] == pytest.approx(2200.0), "子集口径单独暴露供前端自取自算"
    assert r["total"]["total_assets"] is None
    assert r["total"]["total_assets_complete"] is False


def test_summary_full_quotes_total_assets_complete(db, monkeypatch):
    """行情齐全: 总资产才可称为总资产。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0), "000001": _quote(4.0)})
    acc = _account(db, funds=500.0)
    _position(db, acc, "600519", cost=10.0, qty=100)
    _position(db, acc, "000001", cost=5.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]

    assert a["total_assets"] == pytest.approx(2100.0)
    assert a["total_assets_complete"] is True
    assert a["total_assets_basis"] == "all_positions"
    assert a["total_assets_priced_subset"] == pytest.approx(2100.0)


def test_summary_empty_account_coverage_is_consistent(db, monkeypatch):
    """空账户: 无持仓 => 覆盖度完整(0/0), 不能再报 priced_positions_only + incomplete。"""
    _stub_quotes(monkeypatch, {})
    _account(db, funds=800.0)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    cov = r["accounts"][0]["quotes_coverage"]

    assert cov["positions_total"] == 0
    assert cov["positions_priced"] == 0
    assert cov["positions_missing"] == 0
    assert cov["pricing_basis"] == "no_positions"
    assert cov["complete"] is True
    assert cov["cost_coverage_pct"] is None
    assert r["total"]["quotes_coverage"]["complete"] is True


def test_summary_include_quotes_false_coverage_not_missing(db, monkeypatch):
    """不计价模式: 不是「缺了 N 只行情」, 缺失数必须是未知(None)而不是持仓总数。"""
    _stub_quotes(monkeypatch, {})
    acc = _account(db)
    _position(db, acc, "600519", cost=10.0, qty=100)
    _position(db, acc, "000001", cost=5.0, qty=100)

    r = accounts_api.get_portfolio_summary(include_quotes=False, db=db, user=USER_A)
    cov = r["accounts"][0]["quotes_coverage"]

    assert cov["requested"] is False
    assert cov["positions_total"] == 2
    assert cov["positions_priced"] is None
    assert cov["positions_missing"] is None
    assert cov["pricing_basis"] == "not_requested"
    assert cov["complete"] is False
    assert r["total"]["quotes_coverage"]["positions_missing"] is None


def test_summary_nan_inf_price_never_breaks_json(db, monkeypatch):
    """行情坏值 NaN/Inf 不得原样回传(会产出非法 JSON), 也不得参与计价。"""
    import json

    _stub_quotes(
        monkeypatch,
        {
            "600519": _quote(float("nan"), prev_close=float("nan")),
            "000001": _quote(float("inf"), prev_close=float("inf")),
        },
    )
    acc = _account(db)
    _position(db, acc, "600519", cost=10.0, qty=100)
    _position(db, acc, "000001", cost=5.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]
    for pos in a["positions"]:
        assert pos["quote_status"] == "invalid_price"
        assert pos["raw_current_price"] is None, "NaN/Inf 不是可序列化原值"
        assert pos["current_price"] is None and pos["daily_pnl"] is None
    assert a["total_pnl"] is None and a["total_daily_pnl"] is None
    json.dumps(r, allow_nan=False)  # 不抛 = 响应可被浏览器严格解析


def test_merged_holding_basket_and_status_are_order_independent():
    """多账户同标的: 数量与已定价市值必须同篮子, 合并状态不得取决于遍历顺序。"""
    def build(price_first: bool):
        local: dict = {}
        statuses: dict = {}
        legs = [
            {"quantity": 100, "cost_cny": 1000.0, "price": 12.0, "quote_status": "ok"},
            {"quantity": 100, "cost_cny": 1000.0, "price": None, "quote_status": "missing"},
        ]
        if not price_first:
            legs.reverse()
        for leg in legs:
            accounts_api._accumulate_holding(
                local,
                statuses,
                ("CN", "600519"),
                symbol="600519",
                market="CN",
                name="贵州茅台",
                fx=1.0,
                strategy_code="",
                fx_source="live",
                fx_stale=False,
                **leg,
            )
        return local[("CN", "600519")]

    a = build(True)
    b = build(False)
    for h in (a, b):
        assert h["quantity"] == 200
        assert h["priced_quantity"] == 100, "只有已定价仓位能算进市值"
        assert h["unpriced_quantity"] == 100
        assert h["market_value"] == pytest.approx(12.0 * 100), "不得用总数量乘现价"
        assert h["quote_status"] == "partial"
        assert h["position_count"] == 2
        assert h["priced_position_count"] == 1
        assert h["missing_quote_count"] == 1
    assert a["quote_status"] == b["quote_status"]
    assert a["market_value"] == b["market_value"]


def test_merged_holding_all_priced_is_ok_and_none_priced_is_missing():
    local: dict = {}
    statuses: dict = {}
    for price in (12.0, 13.0):
        accounts_api._accumulate_holding(
            local, statuses, ("CN", "600519"), symbol="600519", market="CN", name="贵州茅台",
            quantity=100, fx=1.0, cost_cny=1000.0, price=price, quote_status="ok",
            strategy_code="", fx_source="live", fx_stale=False,
        )
    h = local[("CN", "600519")]
    assert h["quote_status"] == "ok"
    assert h["market_value"] == pytest.approx(2500.0)
    assert h["missing_quote_count"] == 0

    local2: dict = {}
    statuses2: dict = {}
    accounts_api._accumulate_holding(
        local2, statuses2, ("CN", "000001"), symbol="000001", market="CN", name="平安银行",
        quantity=100, fx=1.0, cost_cny=500.0, price=None, quote_status="missing",
        strategy_code="", fx_source="live", fx_stale=False,
    )
    h2 = local2[("CN", "000001")]
    assert h2["quote_status"] == "missing"
    assert h2["market_value"] is None


def test_global_account_hides_other_users_positions(db, monkeypatch):
    """user_id=NULL 的共享账户里可能有别人的 Position, 汇总/诊断/待办都不得看到。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0), "000001": _quote(4.0)})
    shared = _account(db, user_id=None, name="共享")
    _position(db, shared, "600519", cost=10.0, qty=100, user_id=USER_A.id)
    _position(db, shared, "000001", cost=5.0, qty=1000, user_id=USER_B.id)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]
    assert [p["symbol"] for p in a["positions"]] == ["600519"]
    assert a["total_cost"] == pytest.approx(1000.0), "B 的持仓成本不得计入 A"
    assert r["total"]["quotes_coverage"]["positions_total"] == 1

    diag = accounts_api.portfolio_diagnostics(db=db, user=USER_A)
    assert diag["position_count"] == 1
    assert diag["total_market_value"] == pytest.approx(1200.0)

    todos = accounts_api.portfolio_todos(db=db, user=USER_A)
    assert "000001" not in {t.get("symbol") for t in todos["todos"]}


def test_holdings_signature_respects_position_ownership(db, monkeypatch):
    """缓存指纹也必须走同一 SQL 归属门禁, 否则会把别人的持仓算进 A 的组合缓存键。"""
    _stub_quotes(monkeypatch, {})
    shared = _account(db, user_id=None, name="共享")
    st_a = _position(db, shared, "600519", cost=10.0, qty=100, user_id=USER_A.id)
    _position(db, shared, "000001", cost=5.0, qty=1000, user_id=USER_B.id)

    sig = accounts_api._holdings_signature(db, USER_A)
    assert sig == f"{st_a.id}:100"

    holdings = accounts_api._gather_holdings(db, USER_A)
    assert [h["symbol"] for h in holdings] == ["600519"]
    assert holdings[0]["position_count"] == 1


def test_summary_null_available_funds_is_unknown_not_zero(db, monkeypatch):
    """available_funds 为 NULL(老数据): 现金是"未知", 不得当 0 展示, 也不得当 0 加进总资产。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0)})
    acc = _account(db, funds=None)
    # 列默认 default=0 只在 INSERT 缺值时生效; 真正的历史 NULL 要显式 UPDATE 成 NULL。
    acc.available_funds = None
    db.commit()
    _position(db, acc, "600519", cost=10.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]
    assert a["available_funds"] is None, "NULL 现金不得被写成 0(修复前输出 0)"
    assert a["available_funds_known"] is False
    assert a["total_market_value"] == pytest.approx(1200.0), "持仓市值是已知事实, 仍应给出"
    assert a["total_assets"] is None, "现金未知时总资产不可知, 不得按 0 现金凑 1200"
    assert a["total_assets_complete"] is False
    assert a["total_assets_basis"] == "available_funds_unknown"
    assert a["total_assets_priced_subset"] is None

    t = r["total"]
    assert t["available_funds"] is None
    assert t["available_funds_known"] is False
    assert t["total_assets"] is None
    assert t["total_assets_basis"] == "available_funds_unknown"


def test_summary_known_cash_still_folds_into_total_assets(db, monkeypatch):
    """已知现金的账户口径不变: 全持仓有价 → 总资产 = 市值 + 资金, complete=True。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0)})
    acc = _account(db, funds=800.0)
    _position(db, acc, "600519", cost=10.0, qty=100)

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    a = r["accounts"][0]
    assert a["available_funds"] == pytest.approx(800.0)
    assert a["available_funds_known"] is True
    assert a["total_assets"] == pytest.approx(1200.0 + 800.0)
    assert a["total_assets_complete"] is True
    assert r["total"]["total_assets"] == pytest.approx(1200.0 + 800.0)


def test_default_exchange_rate_is_marked_stale(db, monkeypatch):
    """汇率只能用内置默认值时(未成功拉取), 必须标明 source=default / stale=True。"""
    monkeypatch.setattr(
        accounts_api,
        "_hkd_rate_cache",
        {"rate": 0.92, "ts": 0.0, "last_ok_ts": 0.0, "source": "default", "as_of": None},
    )
    monkeypatch.setattr(
        accounts_api,
        "_usd_rate_cache",
        {"rate": 7.25, "ts": 0.0, "last_ok_ts": 0.0, "source": "default", "as_of": None},
    )
    _stub_quotes(monkeypatch, {"00700": _quote(300.0)})
    acc = _account(db)
    _position(db, acc, "00700", cost=200.0, qty=100, market="HK")

    r = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    meta = r["exchange_rate_meta"]["HKD_CNY"]
    assert meta["source"] == "default"
    assert meta["stale"] is True
    assert meta["age_seconds"] is None
    pos = r["accounts"][0]["positions"][0]
    assert pos["exchange_rate_source"] == "default"
    assert pos["exchange_rate_stale"] is True


def test_fetch_fx_rate_records_live_provenance(monkeypatch):
    """拉取成功后必须留下来源与时间戳, 供过期判定; 失败时保留旧来源且标 stale。"""
    from src.web.cache import biz_cache as bc_mod

    class _Resp:
        content = 'v_whHKDCNY="1~港币人民币~HKDCNY~0.9050~0~20261029100000~";'.encode("gbk")

    monkeypatch.setattr(accounts_api.httpx, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(type(bc_mod.biz_cache), "get_json", lambda self, k: None)
    monkeypatch.setattr(type(bc_mod.biz_cache), "set_json", lambda self, *a, **k: None)

    cache = {"rate": 0.92, "ts": 0.0, "last_ok_ts": 0.0, "source": "default", "as_of": None}
    rate = accounts_api._fetch_fx_rate("https://example.invalid/fx", cache, "港币", "fx:test")
    assert rate == pytest.approx(0.905)
    meta = accounts_api._fx_meta(cache, "HKD_CNY")
    assert meta["source"] == "live"
    assert meta["stale"] is False
    assert meta["as_of"]

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(accounts_api.httpx, "get", _boom)
    cache2 = {"rate": 7.25, "ts": 0.0, "last_ok_ts": 0.0, "source": "default", "as_of": None}
    assert accounts_api._fetch_fx_rate("https://example.invalid/fx", cache2, "美元", "fx:test2") == pytest.approx(7.25)
    meta2 = accounts_api._fx_meta(cache2, "USD_CNY")
    assert meta2["source"] == "default" and meta2["stale"] is True


def test_diagnostics_flags_stale_fx_for_foreign_holdings(db, monkeypatch):
    """港美持仓用了默认/过期汇率时, 诊断要显式提示换算不可靠。"""
    monkeypatch.setattr(
        accounts_api,
        "_hkd_rate_cache",
        {"rate": 0.92, "ts": 0.0, "last_ok_ts": 0.0, "source": "default", "as_of": None},
    )
    _stub_quotes(monkeypatch, {"00700": _quote(300.0)})
    acc = _account(db)
    _position(db, acc, "00700", cost=200.0, qty=100, market="HK")

    diag = accounts_api.portfolio_diagnostics(db=db, user=USER_A)
    assert diag["fx_stale_position_count"] == 1
    assert any("汇率" in a for a in diag["alerts"])


def test_owned_account_with_foreign_position_is_isolated(db, monkeypatch):
    """账户归 A 但里面混入 B 的 Position(数据错位): A 只看自己的, B 也看不到 A 的。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0), "000001": _quote(4.0)})
    acc_a = _account(db, user_id=USER_A.id, name="A")
    _position(db, acc_a, "600519", cost=10.0, qty=100, user_id=USER_A.id)
    _position(db, acc_a, "000001", cost=5.0, qty=1000, user_id=USER_B.id)

    r_a = accounts_api.get_portfolio_summary(db=db, user=USER_A)
    assert [p["symbol"] for p in r_a["accounts"][0]["positions"]] == ["600519"]
    assert r_a["total"]["total_cost"] == pytest.approx(1000.0)

    r_b = accounts_api.get_portfolio_summary(db=db, user=USER_B)
    assert r_b["accounts"] == [], "B 不拥有 A 的账户, 不得借错位持仓看到它"


def test_todos_ignore_other_users_alert_rules(db, monkeypatch):
    """待办不能把别人的价格提醒当成"已设提醒"(否则当前用户永远不会被提示)。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0)})
    shared = _account(db, user_id=None, name="共享")
    st = _position(db, shared, "600519", cost=10.0, qty=100, user_id=USER_A.id)
    db.add(
        M.PriceAlertRule(
            user_id=USER_B.id,
            stock_id=st.id,
            name="B 的提醒",
            enabled=True,
        )
    )
    db.commit()

    todos = accounts_api.portfolio_todos(db=db, user=USER_A)
    assert any(t["type"] == "no_alert" and t["symbol"] == "600519" for t in todos["todos"])


def test_gather_holdings_merges_across_accounts_with_consistent_counts(db, monkeypatch):
    """多账户同标的合并后: 数量/市值同篮子, 覆盖度按持仓腿而不是标的数算。"""
    _stub_quotes(monkeypatch, {"600519": _quote(12.0)})
    acc1 = _account(db, user_id=USER_A.id, name="A1")
    acc2 = _account(db, user_id=USER_A.id, name="A2")
    _position(db, acc1, "600519", cost=10.0, qty=100, user_id=USER_A.id)
    _position(db, acc2, "600519", cost=11.0, qty=50, user_id=USER_A.id)

    holdings = accounts_api._gather_holdings(db, USER_A)
    assert len(holdings) == 1
    h = holdings[0]
    assert h["quantity"] == pytest.approx(150.0)
    assert h["priced_quantity"] == pytest.approx(150.0)
    assert h["position_count"] == 2
    assert h["priced_position_count"] == 2
    assert h["quote_status"] == "ok"
    assert h["market_value"] == pytest.approx(12.0 * 150)

    diag = accounts_api.portfolio_diagnostics(db=db, user=USER_A)
    assert diag["holding_count"] == 1
    assert diag["position_count"] == 2
    assert diag["priced_position_count"] == 2
    assert diag["quotes_coverage_pct"] == pytest.approx(100.0)


def test_agent_portfolio_context_isolates_shared_account_positions(db, monkeypatch):
    """Agent 组合上下文读的是同一批表: 共享账户里别人的 Position 也不得混入遗留桶。"""
    from sqlalchemy.orm import sessionmaker

    shared = _account(db, user_id=None, name="共享")
    st_a = _position(db, shared, "600519", cost=10.0, qty=100, user_id=USER_A.id)
    st_b = _position(db, shared, "000001", cost=5.0, qty=100, user_id=USER_B.id)
    st_legacy = _position(db, shared, "600000", cost=3.0, qty=10, user_id=None)
    for st in (st_a, st_b, st_legacy):
        db.add(M.StockAgent(stock_id=st.id, agent_name="scope_probe", schedule=""))
    db.commit()

    import src.bootstrap.runtime as rt

    TestingSession = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    monkeypatch.setattr(rt, "SessionLocal", TestingSession)

    shared_ctx = rt.load_portfolio_for_agent("scope_probe", None)
    shared_symbols = {p.symbol for acc in shared_ctx.accounts for p in acc.positions}
    assert shared_symbols == {"600000"}, "遗留共享桶只含 user_id=NULL 的持仓"

    a_ctx = rt.load_portfolio_for_agent("scope_probe", USER_A.id)
    assert a_ctx.accounts == [], "A 不拥有共享账户, agent 上下文不得借它读取他人持仓"
