"""题材情绪盘中实时刷新 + 收盘定型契约测试(2026-10-08)。

覆盖: phase 五态转换 / 盘中 upsert 刷新(source='intraday') /
15:05 定型幂等(settled_at 不跳变、数值一致) / 定型行不被盘中覆盖 /
陈旧后台刷新不阻塞页面 / 非交易日 / 缺数据显式不编造 / API 新字段契约 /
迁移可重复跑。全部离线(mock 数据源), 不触真实网络。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.core.theme_mood as tm
import src.web.api.theme_mood as api
from src.web.response import ResponseWrapperMiddleware

_CST = timezone(timedelta(hours=8))


# ── in-memory engine(迁移 m163 + m182)────────────────────────────────────────
def _mk_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from src.web.migrations import _m163_theme_mood_table, _m182_theme_mood_settled_at

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    with eng.begin() as conn:
        _m163_theme_mood_table(conn)
        _m182_theme_mood_settled_at(conn)
    return eng


def _rows(eng, sql):
    from sqlalchemy import text

    with eng.begin() as conn:
        return conn.execute(text(sql)).fetchall()


def _exec(eng, sql):
    from sqlalchemy import text

    with eng.begin() as conn:
        conn.execute(text(sql))


@pytest.fixture
def eng(monkeypatch):
    e = _mk_engine()
    monkeypatch.setattr("src.db.session.engine", e)
    return e


# ── phase 五态(纯函数)─────────────────────────────────────────────────────────
@pytest.mark.parametrize("hhmm,trading,settled,want", [
    ((9, 0), True, False, "pre"),               # 盘前
    ((9, 30), True, False, "live"),             # 开盘
    ((12, 0), True, False, "live"),             # 午间仍算盘中
    ((15, 0), True, False, "live"),             # 收盘整点
    ((15, 2), True, False, "closed_pending"),   # 15:00-15:05 未定型窗口
    ((15, 10), True, False, "closed_pending"),  # 过了 15:05 但定型 job 未跑完
    ((15, 10), True, True, "final"),            # 定型完成
    ((9, 0), False, False, "pre"),              # 非交易日
])
def test_resolve_phase_transitions(hhmm, trading, settled, want):
    now = datetime(2026, 10, 8, hhmm[0], hhmm[1])
    phase, note = tm.resolve_phase(now=now, trading_day=trading, settled=settled)
    assert phase == want and isinstance(note, str) and note


def test_resolve_phase_notes_are_explicit_not_masked():
    now = datetime(2026, 10, 8, 15, 10)
    _, pending = tm.resolve_phase(now=now, trading_day=True, settled=False)
    _, final = tm.resolve_phase(now=now, trading_day=True, settled=True)
    assert pending != final
    assert "非交易日" in tm.resolve_phase(
        now=datetime(2026, 10, 6, 10, 0), trading_day=False, settled=False)[1]


def test_is_live_window_uses_calendar():
    assert tm.is_live_window(datetime(2026, 10, 8, 10, 30)) is True    # 交易日盘中
    assert tm.is_live_window(datetime(2026, 10, 8, 9, 0)) is False     # 盘前
    assert tm.is_live_window(datetime(2026, 10, 8, 16, 0)) is False    # 收盘后
    assert tm.is_live_window(datetime(2026, 10, 6, 10, 30)) is False   # 国庆休市
    assert tm.is_live_window(datetime(2026, 10, 10, 10, 30)) is True   # 周六调休补班=交易日


def test_is_settle_due_now():
    assert tm.is_settle_due_now(datetime(2026, 10, 8, 15, 5)) is True
    assert tm.is_settle_due_now(datetime(2026, 10, 8, 14, 0)) is False
    assert tm.is_settle_due_now(datetime(2026, 10, 6, 15, 5)) is False  # 非交易日


def test_intraday_is_stale():
    base = datetime(2026, 10, 8, 10, 0)
    assert tm.intraday_is_stale(None) is True                 # 缺失 → 陈旧(显式触发)
    assert tm.intraday_is_stale("garbage") is True
    assert tm.intraday_is_stale("2026-10-08 09:55:00", now=base) is False   # 5 分钟前
    assert tm.intraday_is_stale("2026-10-08 09:45:00", now=base) is True    # 15 分钟前
    assert tm.intraday_is_stale("2026-10-08T09:55:00+08:00", now=base) is False


# ── market_phase(落库态读取 + 契约)─────────────────────────────────────────
def test_market_phase_no_data_is_live_with_null_as_of(eng):
    """无任何落库: 交易日盘中=live, as_of 显式 null(绝不编时间)。"""
    d = tm.market_phase(now=datetime(2026, 10, 8, 10, 0))
    assert d["phase"] == "live" and d["trading_day"] is True
    assert d["as_of"] is None and d["settled_at"] is None and d["note"]


def test_market_phase_non_trading_day(eng):
    d = tm.market_phase(now=datetime(2026, 10, 6, 10, 0))
    assert d["phase"] == "pre" and d["trading_day"] is False
    assert "非交易日" in d["note"]


def test_market_phase_live_from_intraday_row(eng):
    _exec(eng, "INSERT INTO theme_mood_daily (trade_date, block_code, score, source, updated_at)"
               " VALUES ('20261008','880001.SH',70.0,'intraday','2026-10-08 10:00:00')")
    d = tm.market_phase(now=datetime(2026, 10, 8, 10, 2))
    assert d["phase"] == "live" and d["settled_at"] is None
    assert d["as_of"] == "2026-10-08T10:00:00+08:00"
    assert d["trading_day"] is True


def test_market_phase_final_from_close_row(eng):
    _exec(eng, "INSERT INTO theme_mood_daily (trade_date, block_code, score, source, updated_at, settled_at)"
               " VALUES ('20261008','880001.SH',70.0,'close','2026-10-08 15:05:30','2026-10-08 15:05:30')")
    d = tm.market_phase(now=datetime(2026, 10, 8, 15, 30))
    assert d["phase"] == "final"
    assert d["settled_at"] == "2026-10-08T15:05:30+08:00"
    assert d["as_of"] == "2026-10-08T15:05:30+08:00"


def test_market_phase_closed_pending_windows(eng):
    _exec(eng, "INSERT INTO theme_mood_daily (trade_date, block_code, score, source, updated_at)"
               " VALUES ('20261008','880001.SH',70.0,'intraday','2026-10-08 15:00:10')")
    assert tm.market_phase(now=datetime(2026, 10, 8, 15, 3))["phase"] == "closed_pending"
    assert tm.market_phase(now=datetime(2026, 10, 8, 15, 10))["phase"] == "closed_pending"


# ── scan: 盘中 upsert / 定型幂等 / 定型不被覆盖 / 缺数据显式 ────────────────────
def _patch_scan(monkeypatch, bars_factory):
    monkeypatch.setattr(tm, "sector_items",
                        lambda: [{"code": "880001.SH", "name": "甲题材", "type": "concept"}])
    monkeypatch.setattr(tm, "constituents", lambda c: ["600001.SH"])
    monkeypatch.setattr(tm, "name_map", lambda: {"600001.SH": "甲股"})
    monkeypatch.setattr(tm, "_fetch_bars", bars_factory)


def _bars(dates, pxs):
    def _factory(codes):
        return {c: [
            {"date": d, "open": px * 0.99, "close": px, "high": px * 1.1, "low": px * 0.98,
             "volume": 1000.0, "amount": 1e8}
            for d, px in zip(dates, pxs)] for c in codes}
    return _factory


def test_intraday_scan_writes_source_intraday_without_settle(eng, monkeypatch):
    _patch_scan(monkeypatch, _bars(["20260910", "20260911"], [10.0, 11.0]))
    out = tm.scan(write_days=1, source="intraday", settle=False, min_members=1)
    assert out["ok"] is True and out["source"] == "intraday" and out["settled"] is False
    rows = _rows(eng, "SELECT source, settled_at FROM theme_mood_daily")
    assert len(rows) == 1 and rows[0][0] == "intraday" and rows[0][1] is None


def test_intraday_scan_refreshes_row(eng, monkeypatch):
    """同一天内二次盘中刷新(数据变了)→ 行被刷新(仍 source='intraday')。"""
    _patch_scan(monkeypatch, _bars(["20260910", "20260911"], [10.0, 11.0]))
    tm.scan(write_days=1, source="intraday", settle=False, min_members=1)
    first = _rows(eng, "SELECT score, limit_up_cnt, source FROM theme_mood_daily")[0]
    # 第二次快照: 涨幅变化(11.0→10.2), 结果应刷新
    monkeypatch.setattr(tm, "_fetch_bars", _bars(["20260910", "20260911"], [10.0, 10.2]))
    tm.scan(write_days=1, source="intraday", settle=False, min_members=1)
    second = _rows(eng, "SELECT score, limit_up_cnt, source FROM theme_mood_daily")[0]
    assert second[2] == "intraday"
    assert (second[0], second[1]) != (first[0], first[1])
    assert _rows(eng, "SELECT COUNT(*) FROM theme_mood_daily")[0][0] == 1


def test_settle_idempotent_settled_at_frozen_and_values_consistent(eng, monkeypatch):
    _patch_scan(monkeypatch, _bars(["20260910", "20260911"], [10.0, 11.0]))
    out1 = tm.scan(write_days=1, source="close", settle=True, min_members=1)
    assert out1["ok"] is True and out1["settled"] is True and out1["settled_at"]
    first = _rows(eng, "SELECT score, settled_at, source FROM theme_mood_daily")[0]
    assert first[1] is not None and first[2] == "close"
    # 把 settled_at 改成哨兵(模拟更早的首次定型), 复跑必须保留 → 不跳变
    _exec(eng, "UPDATE theme_mood_daily SET settled_at='2026-01-01 00:00:00'")
    out2 = tm.scan(write_days=1, source="close", settle=True, min_members=1)
    assert out2["ok"] is True
    second = _rows(eng, "SELECT score, settled_at FROM theme_mood_daily")[0]
    assert second[1] == "2026-01-01 00:00:00"   # settled_at 不跳变
    assert second[0] == first[0]                # 数值一致
    assert _rows(eng, "SELECT COUNT(*) FROM theme_mood_daily")[0][0] == 1


def test_settled_row_not_overwritten_by_intraday(eng, monkeypatch):
    """定型(source='close')后, 盘中(source='intraday')逻辑不得覆盖当日行。"""
    _patch_scan(monkeypatch, _bars(["20260910", "20260911"], [10.0, 11.0]))
    tm.scan(write_days=1, source="close", settle=True, min_members=1)
    before = _rows(eng, "SELECT score, limit_up_cnt, source, settled_at FROM theme_mood_daily")[0]
    # 换一组会算出不同分数的行情: 若覆盖就会变
    monkeypatch.setattr(tm, "_fetch_bars", _bars(["20260910", "20260911"], [10.0, 10.2]))
    out = tm.scan(write_days=1, source="intraday", settle=False, min_members=1)
    assert out["ok"] is True
    after = _rows(eng, "SELECT score, limit_up_cnt, source, settled_at FROM theme_mood_daily")[0]
    assert after == before


def test_intraday_scan_no_sector_data_is_explicit(eng, monkeypatch):
    """缺数据必须显式无数据、禁编造(不落任何行)。"""
    monkeypatch.setattr(tm, "sector_items", lambda: [])
    out = tm.scan(write_days=1, source="intraday", settle=False, min_members=1)
    assert out["ok"] is False and "板块目录为空" in out["reason"]
    assert _rows(eng, "SELECT COUNT(*) FROM theme_mood_daily")[0][0] == 0


# ── 迁移可重复跑 ───────────────────────────────────────────────────────────
def test_migration_settled_at_repeatable():
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import StaticPool

    from src.web.migrations import _m163_theme_mood_table, _m182_theme_mood_settled_at

    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    with e.begin() as conn:
        _m163_theme_mood_table(conn)
        _m182_theme_mood_settled_at(conn)
        _m182_theme_mood_settled_at(conn)   # 复跑 no-op, 不报错
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(theme_mood_daily)")).fetchall()]
    assert "settled_at" in cols


# ── API 契约 ───────────────────────────────────────────────────────────────
_ROWS = [{"block_code": "881101.SH", "block_name": "元件", "block_type": "industry",
          "score": 78.2, "delta": 1.0, "confidence": 86, "core": True,
          "s1": 80.0, "s2": 75.0, "s3": 82.0, "s4": 70.0, "s5": 60.0,
          "limit_up_cnt": 9, "core_stocks": [], "cells": []}]


def _client(monkeypatch, latest="20260911", dates=("20260910", "20260911")):
    monkeypatch.setattr(api, "_board_data", lambda w, t: {
        "dates": list(dates), "items": _ROWS,
        "market": [{"date": d, "score": None} for d in dates],
        "rotation": [{"date": d, "new_n": 0, "exit_n": 0, "new_codes": [], "exit_codes": []}
                     for d in dates],
        "rotation_top_k": 10})
    monkeypatch.setattr(api, "_latest_date", lambda: latest)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/theme-mood")
    app.add_middleware(ResponseWrapperMiddleware)
    return TestClient(app)


def test_board_contract_phase_fields(monkeypatch):
    monkeypatch.setattr(api, "_phase_payload", lambda: {
        "phase": "final", "as_of": "2026-10-08T15:05:30+08:00",
        "settled_at": "2026-10-08T15:05:30+08:00", "trading_day": True, "note": "收盘已定型"})
    monkeypatch.setattr(api, "_maybe_revalidate", lambda p: {"triggered": False, "reason": "非盘中"})
    d = _client(monkeypatch).get("/api/theme-mood/board?window=20&top=15").json()["data"]
    assert d["phase"] == "final"
    assert d["as_of"] == "2026-10-08T15:05:30+08:00"
    assert d["settled_at"] == "2026-10-08T15:05:30+08:00"
    assert d["trading_day"] is True and d["note"] == "收盘已定型"
    # 旧字段不回归
    assert d["trade_date"] == "20260911" and d["items"][0]["block_code"] == "881101.SH"


def test_board_live_stale_triggers_background_without_blocking(monkeypatch):
    calls = {"n": 0}

    def _spawn(reason="scheduled"):
        calls["n"] += 1
        return {"started": True, "job_id": "j1", "reason": None}

    monkeypatch.setattr(api, "_phase_payload", lambda: {
        "phase": "live", "as_of": "2026-10-08T09:00:00+08:00",
        "settled_at": None, "trading_day": True, "note": "盘中"})
    monkeypatch.setattr(api, "_spawn_intraday_refresh", _spawn)
    monkeypatch.setattr(api, "_last_revalidate", 0.0)
    d = _client(monkeypatch).get("/api/theme-mood/board").json()["data"]
    assert d["phase"] == "live"                 # 先返回现有值
    assert d["items"]                            # 页面有数据, 未被阻塞
    assert calls["n"] == 1 and d["revalidate"]["triggered"] is True


def test_board_live_fresh_no_trigger(monkeypatch):
    calls = {"n": 0}
    fresh = datetime.now(_CST).isoformat()
    monkeypatch.setattr(api, "_phase_payload", lambda: {
        "phase": "live", "as_of": fresh, "settled_at": None,
        "trading_day": True, "note": "盘中"})
    monkeypatch.setattr(api, "_spawn_intraday_refresh",
                        lambda reason="scheduled": calls.__setitem__("n", calls["n"] + 1) or {"started": True})
    d = _client(monkeypatch).get("/api/theme-mood/board").json()["data"]
    assert calls["n"] == 0 and d["revalidate"]["triggered"] is False


def test_maybe_revalidate_cooldown_throttles(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(api, "_spawn_intraday_refresh",
                        lambda reason="scheduled": calls.__setitem__("n", calls["n"] + 1)
                        or {"started": True, "job_id": "j"})
    monkeypatch.setattr(api, "_last_revalidate", 0.0)
    phase = {"phase": "live", "as_of": "2020-01-01T09:00:00+08:00"}   # 极旧 → stale
    r1 = api._maybe_revalidate(phase)
    r2 = api._maybe_revalidate(phase)
    assert r1["triggered"] is True
    assert r2["reason"] == "cooldown" and calls["n"] == 1


def test_phase_payload_degrades_on_core_error(monkeypatch):
    def _boom(now=None):
        raise RuntimeError("db boom")

    monkeypatch.setattr(tm, "market_phase", _boom)
    out = api._phase_payload()
    assert out["phase"] == "pre" and "降级" in out["note"]
    assert out["as_of"] is None and out["settled_at"] is None and out["trading_day"] is False


def test_intraday_job_skips_off_hours_and_spawns_in_window(monkeypatch):
    monkeypatch.setattr(tm, "is_live_window", lambda now=None: False)
    called = {"n": 0}
    monkeypatch.setattr(api, "_spawn_intraday_refresh",
                        lambda reason="scheduled": called.__setitem__("n", called["n"] + 1) or {})
    assert api.intraday_refresh_job()["skipped"] is True and called["n"] == 0

    monkeypatch.setattr(tm, "is_live_window", lambda now=None: True)
    monkeypatch.setattr(api, "_spawn_intraday_refresh",
                        lambda reason="scheduled": {"started": True, "job_id": "j", "reason": None})
    assert api.intraday_refresh_job()["started"] is True


def test_settle_job_skips_non_trading_and_spawns_when_due(monkeypatch):
    monkeypatch.setattr(tm, "is_settle_due_now", lambda now=None: False)
    called = {"n": 0}
    monkeypatch.setattr(api, "spawn_settle",
                        lambda reason="scheduled": called.__setitem__("n", called["n"] + 1) or {})
    assert api.settle_refresh_job()["skipped"] is True and called["n"] == 0

    monkeypatch.setattr(tm, "is_settle_due_now", lambda now=None: True)
    monkeypatch.setattr(api, "spawn_settle",
                        lambda reason="scheduled": {"started": True, "job_id": "j", "reason": None})
    assert api.settle_refresh_job()["started"] is True
