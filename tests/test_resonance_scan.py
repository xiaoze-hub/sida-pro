"""三指标共振判定 + 全市场扫描测试(2026-09-11, 数智决策升级 A/B/C)。

口径: 趋势(GS: G区/G信号) × 强度(AI机构活跃度>=3) × 资金(主力净流入>0);
三项全对=共振, 恰好两项=接近; 数据缺失项计 False(不猜)。
扫描用通达信批量(日线+SUPAMO 资金), 落库 `(trade_date, symbol)` 唯一幂等。
"""
from __future__ import annotations

import src.core.resonance_scan as rs
import src.db.session as dbs
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from src.web.migrations import _m161_resonance_scan_table, _m162_resonance_ai_verdicts_table


def _mk_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m161_resonance_scan_table(conn)
        _m162_resonance_ai_verdicts_table(conn)
    return eng


def _patch_db(monkeypatch):
    eng = _mk_engine()
    monkeypatch.setattr(dbs, "engine", eng)
    return eng


def _patch_indicators(monkeypatch, trend: str, activity: float | None):
    import src.core.ai_activity as ai
    import src.core.gs_strategy as gs

    monkeypatch.setattr(gs, "eval_gs", lambda bars: {"zone": "G区" if "G" in trend else "S区"})
    monkeypatch.setattr(gs, "trend_label", lambda ev: trend)
    monkeypatch.setattr(ai, "eval_activity", lambda bars: {"activity": activity, "level": "强势" if (activity or 0) >= 3 else "弱"})


def test_evaluate_one_resonance_and_near(monkeypatch):
    bars = [{"date": "2026-09-11", "open": 1.0, "close": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0}]
    _patch_indicators(monkeypatch, "G区间", 4.2)
    out = rs.evaluate_one(bars, 1e8)
    assert out["hits"] == [True, True, True] and out["resonance"] is True and out["near"] is False

    _patch_indicators(monkeypatch, "G信号", 4.2)
    assert rs.evaluate_one(bars, None)["near"] is True  # 资金缺 → 2/3

    _patch_indicators(monkeypatch, "S区间", 4.2)
    out3 = rs.evaluate_one(bars, 1e8)
    assert out3["hits"] == [False, True, True] and out3["resonance"] is False and out3["near"] is True


def test_evaluate_one_missing_data_is_false_not_guessed(monkeypatch):
    _patch_indicators(monkeypatch, "无数据", None)
    out = rs.evaluate_one([], None)
    assert out["hits"] == [False, False, False] and out["resonance"] is False and out["near"] is False


def test_tdx_code_mapping():
    assert rs._tdx_code("600519") == "600519.SH"
    assert rs._tdx_code("000001") == "000001.SZ"
    assert rs._tdx_code("920268") == "920268.BJ"
    assert rs._tdx_code("00700.HK") == "00700.HK"


def test_scan_upserts_and_is_idempotent(monkeypatch):
    eng = _patch_db(monkeypatch)
    monkeypatch.setattr(rs, "_stock_pool", lambda: [("600519.SH", "贵州茅台"), ("000001.SZ", "平安银行")])
    monkeypatch.setattr(
        rs,
        "_fetch_daily",
        lambda codes: {
            c: [{"date": "20260911", "open": 1.0, "close": 2.0, "high": 2.0, "low": 1.0, "volume": 1.0}] for c in codes
        },
    )
    monkeypatch.setattr(rs, "_fetch_funds", lambda codes: {c: 2e8 for c in codes})
    acts = {"600519.SH": 4.5, "000001.SZ": 1.0}
    import src.core.ai_activity as ai
    import src.core.gs_strategy as gs

    monkeypatch.setattr(gs, "eval_gs", lambda bars: {"zone": "G区"})
    monkeypatch.setattr(gs, "trend_label", lambda ev: "G区间")
    monkeypatch.setattr(ai, "eval_activity", lambda bars: {"activity": bars[0].get("__act"), "level": "x"})
    monkeypatch.setattr(
        rs,
        "_fetch_daily",
        lambda codes: {
            c: [{"date": "20260911", "__act": acts[c], "open": 1.0, "close": 2.0, "high": 2.0, "low": 1.0, "volume": 1.0}]
            for c in codes
        },
    )

    out = rs.scan()
    assert out["ok"] is True and out["scanned"] == 2 and out["resonance"] == 1 and out["near"] == 1
    rs.scan()  # 幂等重扫
    with eng.begin() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM resonance_scan")).scalar()
    assert n == 2
    got = rs.latest(only="resonance")
    assert got["count"] == 1 and got["items"][0]["symbol"] == "600519"


def test_daily_job_never_raises(monkeypatch):
    _patch_db(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("tdx down")

    monkeypatch.setattr(rs, "scan", _boom)
    out = rs.daily_job()
    assert out["ok"] is False
