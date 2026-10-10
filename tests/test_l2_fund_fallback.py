# -*- coding: utf-8 -*-
"""L2 成品资金非交易时段回退 + 0 不冒充三态回归(2026-10-11 P1 数据断档修复)。

真 bug: 工作台 L2「L2 成品资金」读 TQ get_more_info **实时会话值**, 非交易时段 TQ
一律回 0 —— UI 未回退未标注, 周末显示"0万 平衡 / 逐笔0笔·委托0笔", 把 0 冒充真实值。

铁律三态(全 mock, 禁真实网络):
- 实时有值 → source='live' 原样返回;
- 非交易时段/会话值全 0 + 有收盘快照 → 回退快照, 显式 source/as_of/note(0 不冒充);
- 非交易时段/会话值全 0 + 无快照 → available:false + 显式无数据文案(绝不返 0)。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core import l2_fund_snapshot as snap  # noqa: E402


# ─────────────── ① 纯逻辑: has_live_values ───────────────

def test_has_live_values():
    assert snap.has_live_values({f: 0 for f in snap.FIELDS}) is False
    assert snap.has_live_values({f: None for f in snap.FIELDS}) is False
    assert snap.has_live_values({"zjl_hb": 0, "l2_tick_num": 120327}) is True
    assert snap.has_live_values({"zjl_hb": -3762.99}) is True
    # bool 不算数值(True is 1 的陷阱)
    assert snap.has_live_values({"zjl_hb": True}) is False


# ─────────────── ② 采样: 全 0 不落, 有值才落 ───────────────

class _MDZero:
    def more_info(self, symbols, *, market="CN"):
        from marketdata.types import MoreInfo
        return [MoreInfo(symbol=s, market="CN", zjl_hb=0.0, l2_tick_num=0,
                         l2_order_num=0, total_buy_vol=0.0, total_sell_vol=0.0,
                         cancel_buy=0.0, cancel_sell=0.0) for s in symbols]


class _MDLive:
    def more_info(self, symbols, *, market="CN"):
        from marketdata.types import MoreInfo
        return [MoreInfo(symbol=s, market="CN", zjl_hb=-3762.99, zjl=-7086.62,
                         l2_tick_num=120327, l2_order_num=224629, total_buy_vol=37842.0,
                         total_sell_vol=96825.0, cancel_buy=11.0, cancel_sell=22.0)
                for s in symbols]


def test_capture_symbol_skips_all_zero(monkeypatch):
    import src.core.marketdata_client as mc

    monkeypatch.setattr(mc, "get_market_data", lambda: _MDZero())
    wrote = []
    monkeypatch.setattr(snap, "_upsert", lambda *a, **k: wrote.append(a))
    r = snap.capture_symbol("002361")
    assert r["captured"] is False and wrote == []  # 全 0 绝不落库


def test_capture_symbol_writes_when_live(monkeypatch):
    import src.core.marketdata_client as mc

    monkeypatch.setattr(mc, "get_market_data", lambda: _MDLive())
    wrote = []
    monkeypatch.setattr(snap, "_upsert", lambda *a, **k: wrote.append(a))
    r = snap.capture_symbol("002361")
    assert r["captured"] is True and len(wrote) == 1
    assert wrote[0][0] == "002361"


# ─────────────── ③ 端点三态(TestClient) ───────────────

def _build_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import quotes as quotes_api

    app = FastAPI()
    app.include_router(quotes_api.router, prefix="/api/quotes")
    return TestClient(app)


def _patch_live(monkeypatch, live_row):
    import src.core.marketdata_client as mc

    monkeypatch.setattr(mc, "md_more_info",
                        lambda syms, market="CN": ([live_row] if live_row else []))


def _patch_session(monkeypatch, in_session: bool):
    import src.core.trading_calendar as tc

    monkeypatch.setattr(tc, "is_trading_session", lambda now=None: in_session)


def _patch_snapshot(monkeypatch, s):
    monkeypatch.setattr(snap, "latest_snapshot", lambda symbol, market="CN": s)


def test_endpoint_live_values_pass_through(monkeypatch):
    _patch_live(monkeypatch, {"symbol": "002361", "zjl_hb": -3762.99, "l2_tick_num": 120327,
                              "l2_order_num": 224629, "total_buy_vol": 37842.0})
    _patch_session(monkeypatch, True)
    _patch_snapshot(monkeypatch, None)

    body = _build_client().get("/api/quotes/002361/more-info").json()
    assert body["available"] is True and body["source"] == "live"
    assert body["zjl_hb"] == -3762.99


def test_endpoint_falls_back_to_snapshot_when_closed(monkeypatch):
    """非交易时段 + 会话值全 0 + 有快照 → 回退并显式标注(0 不冒充)。"""
    _patch_live(monkeypatch, {"symbol": "002361", "zjl_hb": 0.0, "l2_tick_num": 0,
                              "l2_order_num": 0, "total_buy_vol": 0.0})
    _patch_session(monkeypatch, False)
    _patch_snapshot(monkeypatch, {"zjl_hb": -3762.99, "zjl": -7086.62, "total_buy_vol": 37842.0,
                                  "total_sell_vol": 96825.0, "cancel_buy": 11.0,
                                  "cancel_sell": 22.0, "l2_tick_num": 120327,
                                  "l2_order_num": 224629, "trade_date": "20261009",
                                  "origin": "snapshot"})

    body = _build_client().get("/api/quotes/002361/more-info").json()
    assert body["available"] is True and body["source"] == "snapshot"
    assert body["as_of"] == "20261009"
    assert "非交易时段" in body["note"] and "2026-10-09" in body["note"]
    # 回退的是真实快照值, 不是会话的 0
    assert body["zjl_hb"] == -3762.99 and body["l2_tick_num"] == 120327


def test_endpoint_no_snapshot_no_fabrication(monkeypatch):
    """非交易时段 + 全 0 + 无快照 → available:false, 绝不返 0 冒充真实值。"""
    _patch_live(monkeypatch, {"symbol": "002361", "zjl_hb": 0.0, "l2_tick_num": 0})
    _patch_session(monkeypatch, False)
    _patch_snapshot(monkeypatch, None)

    body = _build_client().get("/api/quotes/002361/more-info").json()
    assert body["available"] is False and body["source"] == "session_closed"
    assert "不冒充" in body["note"]


def test_endpoint_seal_sample_fallback_labeled(monkeypatch):
    """回退源为现有落库 seal_quality_samples 时也显式标注(带实际日期)。"""
    _patch_live(monkeypatch, {"symbol": "002361", "zjl_hb": 0.0})
    _patch_session(monkeypatch, False)
    _patch_snapshot(monkeypatch, {"zjl_hb": None, "zjl": None, "total_buy_vol": 37842.0,
                                  "total_sell_vol": 96825.0, "cancel_buy": 1.0,
                                  "cancel_sell": 2.0, "l2_tick_num": 12, "l2_order_num": 20,
                                  "trade_date": "20260930", "origin": "seal_sample"})
    body = _build_client().get("/api/quotes/002361/more-info").json()
    assert body["source"] == "seal_sample" and body["as_of"] == "20260930"
    assert "2026-09-30" in body["note"]


def test_endpoint_missing_live_and_snapshot(monkeypatch):
    """TQ 无实时行 + 无快照 → available:false(不 500, 不编造)。"""
    _patch_live(monkeypatch, None)
    _patch_session(monkeypatch, True)
    _patch_snapshot(monkeypatch, None)
    resp = _build_client().get("/api/quotes/002361/more-info")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False and body["source"] == "unavailable"


# ─────────────── ④ latest_snapshot: 优先新表, 回退 seal(测试库) ───────────────

_SYM = "009998"


@pytest.fixture()
def _cleanup_snap():
    yield
    from sqlalchemy import text

    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM l2_fund_snapshots WHERE symbol = :s"), {"s": _SYM})
        db.execute(text("DELETE FROM seal_quality_samples WHERE symbol = :s"), {"s": _SYM})
        db.commit()
    finally:
        db.close()


def test_latest_snapshot_prefers_table_then_seal(_cleanup_snap):
    from sqlalchemy import text

    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        # 无任何行 → None(不返 0)
        assert snap.latest_snapshot(_SYM, "CN") is None

        # 仅 seal 源 → 回退 seal_sample, 带实际日期
        db.execute(text(
            "INSERT INTO seal_quality_samples (ts, symbol, market, total_buy_vol, l2_tick_num) "
            "VALUES ('2026-09-30T14:58:37+08:00', :s, 'CN', 37842.0, 120327)"
        ), {"s": _SYM})
        db.commit()
        s1 = snap.latest_snapshot(_SYM, "CN")
        assert s1 and s1["origin"] == "seal_sample" and s1["trade_date"] == "20260930"
        assert s1["total_buy_vol"] == 37842.0 and s1["zjl_hb"] is None

        # 新表有行 → 优先新表
        db.execute(text(
            "INSERT INTO l2_fund_snapshots (symbol, market, trade_date, zjl_hb, l2_tick_num) "
            "VALUES (:s, 'CN', '20261009', -3762.99, 120327)"
        ), {"s": _SYM})
        db.commit()
        s2 = snap.latest_snapshot(_SYM, "CN")
        assert s2 and s2["origin"] == "snapshot" and s2["trade_date"] == "20261009"
        assert s2["zjl_hb"] == -3762.99
    finally:
        db.close()
