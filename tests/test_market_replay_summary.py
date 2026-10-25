"""今日盘面播报底料：`GET /api/market/replay-summary`

首页「今日市场播报」的数据源。钉住三件事：
1. **口径必须随数据一起返回**（`sector_caliber`）——板块是「涨停家数聚合」，不是涨幅榜；
   东财板块榜接口在云服务器断连，用户不能误读。
2. **没数据不许返回 0 冒充**——`available=False` + 原因。
3. 读库异常不拖垮首页（返回 available=False）。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.api import market as market_api


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(market_api.router, prefix="/api/market")
    return TestClient(app)


def _fake_row(**kw):
    class Row:
        market = "CN"
        trade_date = "20261023"
        quality = "ok"
        phase = "repair"
        phase_label = "修复"
        limit_up_count = 82
        first_board_count = 70
        ge2_count = 12
        ge3_count = 3
        limit_up_max_streak = 6
        one_word_count = 2
        broken_count = 12
        break_rate_pct = 12.77
        seal_rate_pct = 87.23
        promo_rate_pct = 36.36
        limit_down_count = 6
        mainline = {
            "top_sectors": [
                {"name": "专用设备", "count": 8},
                {"name": "软件开发", "count": 7},
            ],
            "leaders": [
                {"sector": "专用设备", "symbol": "002535", "name": "林州重机",
                 "board_days": 2, "first_seal": "09:30:00"},
            ],
        }

    for k, v in kw.items():
        setattr(Row, k, v)
    return Row()


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._row


class _FakeDB:
    def __init__(self, row):
        self._row = row
        self.closed = False

    def query(self, *a, **k):
        return _FakeQuery(self._row)

    def close(self):
        self.closed = True


def _patch_db(monkeypatch, db):
    import src.db.session as session_mod

    monkeypatch.setattr(session_mod, "SessionLocal", lambda: db)


def test_有数据时返回播报底料(client, monkeypatch):
    _patch_db(monkeypatch, _FakeDB(_fake_row()))
    r = client.get("/api/market/replay-summary")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is True
    assert d["trade_date"] == "20261023"
    assert d["limit_up_count"] == 82
    assert d["ge2_count"] == 12
    assert d["phase_label"] == "修复"
    assert len(d["top_sectors"]) == 2
    assert d["top_sectors"][0]["name"] == "专用设备"
    assert d["leaders"][0]["name"] == "林州重机"


def test_必须带板块口径_防止被读成涨幅榜(client, monkeypatch):
    _patch_db(monkeypatch, _FakeDB(_fake_row()))
    d = client.get("/api/market/replay-summary").json()
    assert "涨停家数" in d["sector_caliber"]
    assert "非板块涨跌幅" in d["sector_caliber"]


def test_没数据返回_available_False_不返回0冒充(client, monkeypatch):
    _patch_db(monkeypatch, _FakeDB(None))
    d = client.get("/api/market/replay-summary").json()
    assert d["available"] is False
    assert "reason" in d and d["reason"]
    assert "limit_up_count" not in d, "无数据时不得出现 0 值字段，避免被当真实数据"


def test_读库异常不拖垮首页(client, monkeypatch):
    import src.db.session as session_mod

    class Boom:
        def query(self, *a, **k):
            raise RuntimeError("db down")

        def close(self):
            pass

    monkeypatch.setattr(session_mod, "SessionLocal", lambda: Boom())
    d = client.get("/api/market/replay-summary").json()
    assert d["available"] is False
    assert d["reason"] == "盘面数据读取失败"


def test_mainline_是字符串时也能解析(client, monkeypatch):
    import json as _json

    row = _fake_row()
    row.mainline = _json.dumps(row.mainline, ensure_ascii=False)
    _patch_db(monkeypatch, _FakeDB(row))
    d = client.get("/api/market/replay-summary").json()
    assert d["top_sectors"][0]["name"] == "专用设备"


def test_板块最多6个_龙头最多5个(client, monkeypatch):
    row = _fake_row()
    row.mainline = {
        "top_sectors": [{"name": f"板块{i}", "count": 20 - i} for i in range(9)],
        "leaders": [{"sector": "x", "symbol": f"00000{i}", "name": f"股{i}",
                     "board_days": 1, "first_seal": "09:30:00"} for i in range(8)],
    }
    _patch_db(monkeypatch, _FakeDB(row))
    d = client.get("/api/market/replay-summary").json()
    assert len(d["top_sectors"]) == 6
    assert len(d["leaders"]) == 5
