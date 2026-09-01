# -*- coding: utf-8 -*-
"""全市场三榜扫描 API 接线 单测: src/web/api/market_scan.py

覆盖:
  - GET /ranks: 无快照 → available=false + note(不编造); 有快照 → 透传 payload
  - POST /refresh: 限池扫描 + 落库; 扫描异常 → 500
  - run_market_scan_job: 扫描 + 落库(内部自开 session); 扫描失败 → {ok:false}
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.web.api import market_scan as ms  # noqa: E402


# ---------------------------------------------------------------------------
# 工具: 构造 FastAPI TestClient(带 DB override)
# ---------------------------------------------------------------------------
def _make_client(db_rows=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *a, **k):
            return self

        def order_by(self, *a):
            return self

        def first(self):
            return self._rows[0] if self._rows else None

    class FakeDB:
        def __init__(self, rows):
            self._rows = rows
            self.added = []

        def query(self, model):
            return FakeQuery(self._rows)

        def add(self, obj):
            self.added.append(obj)

        def commit(self):
            pass

        def rollback(self):
            pass

    app = FastAPI()
    app.include_router(ms.router, prefix="/api/market-scan")
    app.dependency_overrides[ms.get_db] = lambda: FakeDB(db_rows or [])
    return TestClient(app)


def _rank_row(payload):
    from datetime import datetime

    class Row:
        snapshot_date = "2026-09-01"
        stock_market = "CN"
        updated_at = datetime(2026, 9, 1, 15, 30, 0)

    Row.payload = payload  # class 体作用域看不到外层参数, 定义后再赋值
    return Row()


# ---------------------------------------------------------------------------
# GET /ranks
# ---------------------------------------------------------------------------
def test_get_ranks_no_snapshot():
    r = _make_client([]).get("/api/market-scan/ranks")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert "暂无" in (body.get("note") or "")


def test_get_ranks_with_snapshot():
    payload = {
        "generated_at": "2026-09-01T15:30:00",
        "universe": 100, "computed": 90, "skipped": 10,
        "new_g_points": [{"symbol": "000001", "close": 10.5}],
        "dark_top": [{"symbol": "000002", "dark_net": 123456.7, "approximation": True}],
        "activity_top": [{"symbol": "000003", "activity": 6.5, "level": "大牛线"}],
        "zljc": None,
    }
    r = _make_client([_rank_row(payload)]).get("/api/market-scan/ranks")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["snapshot_date"] == "2026-09-01"
    assert len(body["new_g_points"]) == 1
    # 暗盘对照项 approximation 原样透传(诚实口径)
    assert body["dark_top"][0]["approximation"] is True


# ---------------------------------------------------------------------------
# POST /refresh
# ---------------------------------------------------------------------------
def test_refresh_calls_scan_and_limits(monkeypatch):
    """限池扫描: symbols 传入 scan(), 返回结果并标记 available。"""
    import src.core.market_scan as core_scan

    seen = {}

    def fake_scan(symbols=None, top_n=20, bars_days=60, dark_days=1, with_zljc=True):
        seen["symbols"] = symbols
        seen["top_n"] = top_n
        return {
            "generated_at": "x", "universe": 2, "computed": 2, "skipped": 0,
            "new_g_points": [], "dark_top": [], "activity_top": [], "zljc": None,
        }

    monkeypatch.setattr(core_scan, "scan", fake_scan)

    client = _make_client([])
    r = client.post("/api/market-scan/refresh", json={"symbols": ["000001", "000002"], "top_n": 5})
    assert r.status_code == 200
    body = r.json()
    assert seen["symbols"] == ["000001", "000002"]
    assert seen["top_n"] == 5
    assert body["available"] is True


def test_refresh_scan_error_500(monkeypatch):
    import src.core.market_scan as core_scan

    monkeypatch.setattr(
        core_scan, "scan",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    r = _make_client([]).post("/api/market-scan/refresh", json={})
    assert r.status_code == 500


# ---------------------------------------------------------------------------
# run_market_scan_job
# ---------------------------------------------------------------------------
def test_run_job_scan_failure_returns_ok_false(monkeypatch):
    import src.core.market_scan as core_scan

    monkeypatch.setattr(
        core_scan, "scan",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    r = ms.run_market_scan_job()
    assert r["ok"] is False
    assert "boom" in r["error"]


def test_run_job_success(monkeypatch):
    import src.core.market_scan as core_scan
    import src.web.database as dbmod

    monkeypatch.setattr(
        core_scan, "scan",
        lambda **kw: {
            "universe": 3, "computed": 3, "skipped": 0,
            "new_g_points": [{"symbol": "a"}],
            "dark_top": [{"symbol": "b"}],
            "activity_top": [{"symbol": "c"}],
            "zljc": None,
        },
    )

    class FakeQuery:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None  # 无旧快照 → 走 add

    class FakeDB:
        def __init__(self):
            self.added = []

        def query(self, model):
            return FakeQuery()

        def add(self, obj):
            self.added.append(obj)

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    fake_db = FakeDB()
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: fake_db)
    r = ms.run_market_scan_job()
    assert r["ok"] is True
    assert r["new_g"] == 1
    assert r["dark_top"] == 1
    assert r["activity_top"] == 1
    assert len(fake_db.added) == 1  # 落库一行
