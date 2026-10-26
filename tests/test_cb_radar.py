"""I-4 可转债-正股联动套利雷达 单元测试(2026-10-18)。

覆盖:
- 指标推导: 转股价值 / 溢价率 / 双低值 / 强赎距离 / 纯债溢价 / 滞后幅度
- **缺失即 None**, 绝不填 0 或估算(A-06 同族铁律: 与客户端对不上就是信任崩塌)
- 场景判定: 正股封板转债滞后 / 折价套利 / 强赎触发 / 接近强赎 / 双低前列
- 排序与双低排名
- 编排层: 数据源不可用 → available=False + note(不伪造行)
- 合规: 模块源码不含主观推荐/违禁词
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from src.core import cb_radar as cr

_MODULE = Path(cr.__file__).read_text(encoding="utf-8")
_BANNED = re.compile(r"主力建仓|主力出货|建议买入|建议卖出|必涨|稳赚")


@pytest.fixture(autouse=True)
def _clean_terms():
    cr.clear_terms_cache()
    yield
    cr.clear_terms_cache()


# ── 指标推导 ────────────────────────────────────────────────────────────
def test_metrics_happy_path():
    m = cr.compute_metrics(
        bond_price=120.0,
        bond_prev_close=118.0,
        conv_price=10.0,
        stock_price=11.0,
        stock_prev_close=10.8,
        force_redeem=13.0,
        pure_bond=90.0,
    )
    assert m["conv_value"] == pytest.approx(110.0)
    assert m["premium_pct"] == pytest.approx(round((120 / 110 - 1) * 100, 2))
    assert m["double_low"] == pytest.approx(round(120 + m["premium_pct"], 2))
    assert m["redeem_distance_pct"] == pytest.approx(round((11 / 13 - 1) * 100, 2))
    assert m["pure_bond_premium_pct"] == pytest.approx(round((120 / 90 - 1) * 100, 2))
    assert m["bond_chg_pct"] == pytest.approx(round((120 / 118 - 1) * 100, 2))
    assert m["missing"] == []


def test_metrics_missing_conv_price_never_estimates():
    """转股价缺失: 转股价值/溢价率/双低一律 None, 不拿债价顶替。"""
    m = cr.compute_metrics(
        bond_price=120.0,
        bond_prev_close=118.0,
        conv_price=None,
        stock_price=11.0,
        stock_prev_close=10.8,
        force_redeem=13.0,
        pure_bond=90.0,
    )
    assert m["conv_value"] is None
    assert m["premium_pct"] is None
    assert m["double_low"] is None
    assert {"conv_value", "premium_pct"} <= set(m["missing"])


def test_metrics_missing_pure_bond_and_force_redeem():
    m = cr.compute_metrics(
        bond_price=120.0,
        bond_prev_close=118.0,
        conv_price=10.0,
        stock_price=11.0,
        stock_prev_close=10.8,
        force_redeem=None,
        pure_bond=None,
    )
    assert m["redeem_distance_pct"] is None
    assert m["pure_bond_premium_pct"] is None
    assert "redeem_distance" in m["missing"] and "pure_bond_premium" in m["missing"]


def test_lag_is_conversion_value_move_minus_bond_move():
    """正股 +10%, 转股价值 +10%, 转债只 +2% ⇒ 滞后 8 个点。"""
    m = cr.compute_metrics(
        bond_price=102.0,
        bond_prev_close=100.0,
        conv_price=10.0,
        stock_price=11.0,
        stock_prev_close=10.0,
        force_redeem=15.0,
        pure_bond=95.0,
    )
    assert m["conv_chg_pct"] == pytest.approx(10.0)
    assert m["bond_chg_pct"] == pytest.approx(2.0)
    assert m["lag_pct"] == pytest.approx(8.0)


# ── 场景判定 ────────────────────────────────────────────────────────────
def _row(**over):
    base = {
        "code": "113550",
        "stock_code": "600111",
        "bond_chg_pct": 2.0,
        "conv_chg_pct": 10.0,
        "lag_pct": 8.0,
        "premium_pct": 12.0,
        "redeem_distance_pct": -30.0,
        "stock_price": 11.0,
        "force_redeem_price": 15.0,
        "stock_chg_pct": 10.0,
        "double_low": 130.0,
        "double_low_rank": 5,
        "double_low_top": False,
    }
    base.update(over)
    return base


def test_scenario_stock_limit_up_lag():
    keys = {s["key"] for s in cr.classify_scenarios(_row())}
    assert "stock_limit_up_lag" in keys


def test_scenario_lag_not_triggered_for_chinext_at_10pct():
    """创业板 20cm: 涨 10% 不算涨停, 不得报"封板滞后"。"""
    row = _row(stock_code="300750", stock_chg_pct=10.0)
    keys = {s["key"] for s in cr.classify_scenarios(row)}
    assert "stock_limit_up_lag" not in keys


def test_scenario_discount_arb():
    keys = {s["key"] for s in cr.classify_scenarios(_row(premium_pct=-1.5))}
    assert "discount_arb" in keys


def test_scenario_discount_boundary_exact_minus_one_not_triggered():
    keys = {s["key"] for s in cr.classify_scenarios(_row(premium_pct=-0.99))}
    assert "discount_arb" not in keys


def test_scenario_force_redeem_breached_and_near():
    keys = {s["key"] for s in cr.classify_scenarios(_row(redeem_distance_pct=2.5))}
    assert "force_redeem_breached" in keys
    keys = {s["key"] for s in cr.classify_scenarios(_row(redeem_distance_pct=-5.0))}
    assert "force_redeem_near" in keys


def test_scenario_double_low_top():
    keys = {s["key"] for s in cr.classify_scenarios(_row(double_low_top=True))}
    assert "double_low_top" in keys


def test_scenario_does_not_invent_when_metrics_missing():
    """全 None 时不产出任何场景(尤其不能默认判"接近强赎")。"""
    row = _row(premium_pct=None, redeem_distance_pct=None, lag_pct=None, stock_chg_pct=None)
    assert cr.classify_scenarios(row) == []


# ── 行组装 / 排序 / 双低排名 ─────────────────────────────────────────────
def _terms(name="转债A", conv=10.0, force=13.0, real=90.0, stock="600111"):
    return {
        "KZZCode": "113550",
        "KZZName": name,
        "KZZNow": 0,
        "HSCode": stock,
        "ZGPrice": conv,
        "ForceRedeem": force,
        "RealValue": real,
        "HSScore": "AA",
        "RestScope": 12.5,
        "EndDate": "2029-06-30",
        "_stock_code": stock,
    }


def test_build_rows_sorts_risk_first_then_premium():
    terms = {
        "110001": _terms(name="风险券", conv=10.0, force=10.5),
        "110002": _terms(name="折价券", conv=10.0, force=20.0),
        "110003": _terms(name="普通券", conv=10.0, force=20.0),
    }
    bond_quotes = {
        "110001": {"Now": 120.0, "LastClose": 119.0},
        "110002": {"Now": 100.0, "LastClose": 100.0},
        "110003": {"Now": 130.0, "LastClose": 130.0},
    }
    stocks = {"600111": {"Now": 11.0, "LastClose": 11.0}}
    rows = cr.build_rows(terms=terms, bond_quotes=bond_quotes, stock_quotes=stocks)

    assert [r["code"] for r in rows][0] == "110001"   # 强赎触发区(=正股 11 ≥ 10.5)
    assert rows[0]["scenario_keys"][0] == "force_redeem_breached"
    # 折价券(溢价 -9.09%)排在普通券(溢价 18%)之前
    assert [r["code"] for r in rows][1] == "110002"


def test_build_rows_double_low_rank_only_on_computable_rows():
    terms = {
        "110001": _terms(conv=None),      # 无法算溢价 → 不参与双低排名
        "110002": _terms(conv=10.0),
        "110003": _terms(conv=10.0),
    }
    bond_quotes = {
        "110001": {"Now": 100.0},
        "110002": {"Now": 105.0},
        "110003": {"Now": 120.0},
    }
    stocks = {"600111": {"Now": 11.0}}
    rows = cr.build_rows(terms=terms, bond_quotes=bond_quotes, stock_quotes=stocks, double_low_top_n=1)
    ranked = [r for r in rows if r.get("double_low_top")]
    assert len(ranked) == 1 and ranked[0]["code"] == "110002"
    assert next(r for r in rows if r["code"] == "110001").get("double_low_rank") is None


def test_build_rows_falls_back_to_terms_price_when_quote_missing():
    terms = {"110001": {**_terms(), "KZZNow": 118.0}}
    rows = cr.build_rows(terms=terms, bond_quotes={}, stock_quotes={})
    assert rows[0]["bond_price"] == 118.0
    # 但正股报价缺失 ⇒ 转股价值仍是 None(不拿债价推)
    assert rows[0]["conv_value"] is None


# ── 编排层(数据源注入) ───────────────────────────────────────────────────
def _fake_deps(monkeypatch, *, universe=("110001", "110002")):
    monkeypatch.setattr(cr, "_universe", lambda: list(universe))

    def _quotes(codes, chunk=500):
        return {
            "110001": {"Now": 120.0, "LastClose": 118.0, "Zaf": 1.7},
            "110002": {"Now": 100.0, "LastClose": 100.0, "Zaf": 0.0},
            "600111": {"Now": 11.0, "LastClose": 10.8, "Zaf": 1.85},
        }

    def _terms_of(code):
        return {
            "110001": {**_terms(name="转债一"), "_stock_code": "600111"},
            "110002": {**_terms(name="转债二", conv=10.0, force=20.0), "_stock_code": "600111"},
        }.get(code, {})

    monkeypatch.setattr(cr, "_batch_quotes", _quotes)
    monkeypatch.setattr(cr, "_terms", _terms_of)


def test_build_radar_available(monkeypatch):
    _fake_deps(monkeypatch)
    p = cr.build_radar(limit=10, now=datetime.fromisoformat("2026-10-16 15:30:00"))
    assert p["available"] is True
    assert p["evaluated"] == 2
    assert p["universe"] == 2
    assert p["trade_phase"] == "post_close"
    assert p["data_date"] == "2026-10-16"
    assert p["is_live"] is False          # 收盘后不许自称实时
    assert p["is_today"] is True
    assert all(r["conv_value"] is not None for r in p["rows"])
    assert p["caliber"].startswith("转股价")


def test_build_radar_degraded_when_universe_empty(monkeypatch):
    monkeypatch.setattr(cr, "_universe", lambda: [])
    p = cr.build_radar(limit=10)
    assert p["available"] is False and p["rows"] == []
    assert "未返回可转债列表" in p["note"]


def test_build_radar_degraded_when_quotes_missing(monkeypatch):
    monkeypatch.setattr(cr, "_universe", lambda: ["110001"])
    monkeypatch.setattr(cr, "_batch_quotes", lambda *a, **k: {})
    p = cr.build_radar(limit=10)
    assert p["available"] is False
    assert "批量行情不可用" in p["note"]


def test_build_radar_degraded_when_terms_missing(monkeypatch):
    monkeypatch.setattr(cr, "_universe", lambda: ["110001"])
    monkeypatch.setattr(cr, "_batch_quotes", lambda *a, **k: {"110001": {"Now": 120.0}})
    monkeypatch.setattr(cr, "_terms", lambda code: {})
    p = cr.build_radar(limit=10)
    assert p["available"] is False
    assert "条款不可用" in p["note"]


def test_build_one_rejects_bad_code():
    p = cr.build_one("abc")
    assert p["available"] is False and "非法转债代码" in p["note"]


def test_build_one_ok(monkeypatch):
    monkeypatch.setattr(cr, "_terms", lambda c: {**_terms(name="转债一"), "_stock_code": "600111"})
    monkeypatch.setattr(
        cr, "_batch_quotes", lambda codes, chunk=500: {"110001": {"Now": 120.0, "LastClose": 118.0}, "600111": {"Now": 11.0}}
    )
    p = cr.build_one("110001", now=datetime.fromisoformat("2026-10-16 10:30:00"))
    assert p["available"] is True
    assert p["row"]["code"] == "110001"
    assert p["row"]["premium_pct"] is not None
    assert p["trade_phase"] == "continuous"


def test_module_has_no_sales_pitch():
    hits = [(i, ln.strip()) for i, ln in enumerate(_MODULE.splitlines(), 1) if _BANNED.search(ln)]
    assert hits == [], f"命中违禁/推荐措辞: {hits}"
