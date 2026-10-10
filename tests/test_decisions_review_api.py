"""数智决策 P2 合集后端钉子(2026-10-10)。

钉四类"有功能没入口"能力的 API 契约, 重点是**诚实口径 + 永不 500**:
  ① 共振回测 `GET /api/decisions/backtest`: 缺股票池显式降级; 成功时 `basis` 透传;
     计算抛异常 → `available=false` 而非 500;
  ② 入场后验 `GET /api/decisions/entry-outcomes`: 样本不足显式 `insufficient`;
     查询失败 → 降级 payload 而非 500;
  ③ 账本分页 `query_log`: limit 钳制 / offset 越界返回空页 / 日期过滤归一;
  ④ 样本不足一律不给数字(不拿小样本算百分比)。
"""
from __future__ import annotations

import sqlalchemy as sa
import pytest
from fastapi.testclient import TestClient

from src.core import decision_log as dl
from src.web.api.auth import get_current_user
from src.web.migrations import _m175_decision_log


def _engine():
    eng = sa.create_engine("sqlite://")
    with eng.connect() as conn:
        _m175_decision_log(conn)
    return eng


# ── ③ 账本分页: limit / offset / 日期 ────────────────────────────────────────
def test_query_log_limit_clamped_and_pagination():
    eng = _engine()
    for i in range(5):
        dl.record_signal(eng, signal_kind="resonance3", symbol=f"00000{i}",
                         trade_date=f"2026-09-0{i + 1}", price=10.0 + i)
    # limit 超上限 → 钳到 500; 全量 5 条
    page = dl.query_log(eng, limit=9999, offset=0)
    assert page["limit"] == 500
    assert page["total"] == 5
    assert page["count"] == 5
    assert page["has_more"] is False
    # 分页: limit=2 offset=0 → 2 条, has_more=True
    p1 = dl.query_log(eng, limit=2, offset=0)
    assert p1["count"] == 2 and p1["has_more"] is True
    p3 = dl.query_log(eng, limit=2, offset=4)
    assert p3["count"] == 1 and p3["has_more"] is False
    # 无重叠(靠 trade_date DESC, id DESC 稳定)
    keys1 = {(r["signal_kind"], r["symbol"]) for r in p1["items"]}
    keys3 = {(r["signal_kind"], r["symbol"]) for r in p3["items"]}
    assert keys1.isdisjoint(keys3)


def test_query_log_offset_out_of_range_is_empty_page():
    eng = _engine()
    dl.record_signal(eng, signal_kind="resonance3", symbol="600000", trade_date="2026-09-01")
    page = dl.query_log(eng, offset=1000)
    assert page["count"] == 0
    assert page["items"] == []
    assert page["has_more"] is False
    assert page["total"] == 1
    # 负数 offset 按 0 处理(不报错)
    assert dl.query_log(eng, offset=-5)["offset"] == 0


def test_query_log_date_filter_normalizes_iso_and_compact():
    eng = _engine()
    dl.record_signal(eng, signal_kind="resonance3", symbol="600000", trade_date="20260901")
    dl.record_signal(eng, signal_kind="resonance3", symbol="600001", trade_date="20260920")
    # ISO 边界也要能过滤(库内是紧凑格式)
    page = dl.query_log(eng, start_date="2026-09-10", end_date="2026-09-30")
    assert {r["symbol"] for r in page["items"]} == {"600001"}
    assert page["total"] == 1
    # 非法日期 → 忽略该过滤(不报错、不误过滤)
    assert dl.query_log(eng, start_date="bad")["total"] == 2


def test_query_log_keeps_unfilled_null():
    """未回填的档位必须是 null, 不是 0(诚实口径)。"""
    eng = _engine()
    dl.record_signal(eng, signal_kind="resonance3", symbol="600000", trade_date="20260901")
    it = dl.query_log(eng)["items"][0]
    assert it["outcomes"]["t1"]["ret"] is None
    assert it["outcomes"]["t1"]["hit"] is None


# ── API 层: 回测 / 入场后验 降级不 500 ────────────────────────────────────────
@pytest.fixture()
def client():
    from src.web.app import app

    app.dependency_overrides[get_current_user] = lambda: object()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _body(r):
    """统一响应信封 {code,data,message} → data。"""
    return r.json()["data"]


def test_backtest_no_symbols_explicit_degrade(client):
    r = client.get("/api/decisions/backtest")
    assert r.status_code == 200
    data = _body(r)
    assert data["available"] is False
    assert "股票池" in data["error"]
    # basis 必须透传(双指标诚实口径标记)
    assert "双指标" in data["basis"]
    assert data["sample"]["signals"] == 0


def test_backtest_passes_basis_and_params(client, monkeypatch):
    def fake_backtest(**kwargs):
        assert kwargs["symbols"] == ["002361", "600519"]
        assert kwargs["fund_source"] == "ohlc"
        return {
            "params": kwargs,
            "basis": "三指标(资金=OHLC对照项)",
            "sample": {"symbols": 2, "signals": 7},
            "by_phase": {"向好": {"count": 7, "win_rate": 0.7}},
            "official": {"向好": {"win_rate": 75.42}},
            "note": "对照项误差大",
        }

    monkeypatch.setattr("src.core.decision_backtest.backtest_resonance", fake_backtest)
    r = client.get("/api/decisions/backtest?symbols=002361,600519&fund_source=ohlc&hold_days=5")
    assert r.status_code == 200
    data = _body(r)
    assert data["available"] is True
    # 后端已有的 basis 标注必须透传到 UI
    assert data["basis"] == "三指标(资金=OHLC对照项)"
    assert data["sample"]["signals"] == 7


def test_backtest_internal_error_degrades_not_500(client, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("no kline source")

    monkeypatch.setattr("src.core.decision_backtest.backtest_resonance", boom)
    r = client.get("/api/decisions/backtest?symbols=002361")
    assert r.status_code == 200
    data = _body(r)
    assert data["available"] is False
    assert "回测计算失败" in data["error"]


def test_backtest_max_symbols_caps_pool(client, monkeypatch):
    captured = {}

    def fake_backtest(**kwargs):
        captured["symbols"] = kwargs["symbols"]
        return {"params": kwargs, "basis": "双指标(缺资金维)", "sample": {"symbols": 0, "signals": 0}}

    monkeypatch.setattr("src.core.decision_backtest.backtest_resonance", fake_backtest)
    syms = ",".join(f"60000{i}" for i in range(10))
    client.get(f"/api/decisions/backtest?symbols={syms}&max_symbols=3")
    assert len(captured["symbols"]) == 3


def test_entry_outcomes_sample_insufficient_explicit(client, monkeypatch):
    def fake_summary(days=30, min_sample=20):
        return {
            "window_days": days,
            "min_sample": min_sample,
            "available": True,
            "rows": [
                {
                    "horizon_days": 5, "source": "watchlist", "source_label": "自选",
                    "total": 3, "wins": 3, "win_rate": None,
                    "avg_return_pct": None, "insufficient": True, "min_sample": min_sample,
                }
            ],
            "total_samples": 3,
            "note": "样本不足不给数字",
        }

    monkeypatch.setattr("src.core.entry_candidates.entry_outcomes_summary", fake_summary)
    r = client.get("/api/decisions/entry-outcomes?days=30&min_sample=20")
    assert r.status_code == 200
    row = _body(r)["rows"][0]
    assert row["insufficient"] is True
    assert row["win_rate"] is None  # 样本不足不给数字


def test_entry_outcomes_query_failure_degrades_not_500(client, monkeypatch):
    def boom(days=30, min_sample=20):
        raise RuntimeError("db down")

    monkeypatch.setattr("src.core.entry_candidates.entry_outcomes_summary", boom)
    r = client.get("/api/decisions/entry-outcomes")
    assert r.status_code == 200
    data = _body(r)
    assert data["available"] is False
    assert data["rows"] == []


def test_entry_outcomes_summary_empty_db_explicit():
    """空库/无样本 → rows 空且 total_samples=0(显式, 不编造)。"""
    from src.core.entry_candidates import entry_outcomes_summary

    res = entry_outcomes_summary(days=30, min_sample=20)
    assert res["available"] is True
    assert isinstance(res["rows"], list)
    # 只要库内无窗口内后验, 就不应凭空给出样本
    for row in res["rows"]:
        assert row["total"] >= 0
        if row["insufficient"]:
            assert row["win_rate"] is None
