"""大盘资金流快照采样器测试(2026-09-11): 时段守卫 / 取数失败不抛 / 成功落库。

背景: 快照原先只在前端调用大盘资金流接口时写入 → 页面不开则日内曲线空白("没内容")。
"""
from __future__ import annotations

import src.db.session as dbs
import src.core.market_flow_sampler as sampler
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from src.web.migrations import _m138_market_flow_snapshots_table


def _mk_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m138_market_flow_snapshots_table(conn)
    return eng


def _patch_db(monkeypatch):
    eng = _mk_engine()
    monkeypatch.setattr(dbs, "engine", eng)
    return eng


def _count(eng) -> int:
    with eng.begin() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM market_flow_snapshots")).scalar() or 0)


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


def test_skips_outside_trading_window(monkeypatch):
    eng = _patch_db(monkeypatch)
    import src.core.quote_snapshots as qs

    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: False)
    called = {"n": 0}

    def _req_get(*a, **k):
        called["n"] += 1
        raise AssertionError("非交易时段不应发请求")

    import requests

    monkeypatch.setattr(requests, "get", _req_get)
    out = sampler.collect_once()
    assert out["ok"] is True and out["skipped"] is True
    assert called["n"] == 0 and _count(eng) == 0


def test_fetch_failure_is_silent(monkeypatch):
    eng = _patch_db(monkeypatch)
    import src.core.quote_snapshots as qs

    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: True)
    import requests

    def _boom(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(requests, "get", _boom)
    out = sampler.collect_once()
    assert out["ok"] is False and "gateway down" in out["error"]
    assert _count(eng) == 0


def test_success_writes_snapshot_with_gateway_units(monkeypatch):
    eng = _patch_db(monkeypatch)
    import src.core.quote_snapshots as qs

    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: True)
    import requests

    payload = {
        "total_main_flow": -695.9,  # 亿
        "up_count": 333,
        "down_count": 4926,
        "flat_count": 25,
        "sh": {"main_flow": -325.3},
        "sz": {"main_flow": -370.6},
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))
    out = sampler.collect_once()
    assert out["ok"] is True and out["skipped"] is False
    assert _count(eng) == 1
    with eng.begin() as conn:
        row = conn.execute(
            text("SELECT total_main_flow, up_count, down_count, sh_flow, sz_flow FROM market_flow_snapshots")
        ).fetchone()
    assert row[0] == -695.9 and row[1] == 333 and row[2] == 4926
    assert row[3] == -325.3 and row[4] == -370.6
