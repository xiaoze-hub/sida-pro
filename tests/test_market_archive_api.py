"""数据落库面查询 API(批次3, 2026-09-10): 只读路由 + 边界校验 + 新鲜度面板。

真实 sqlite 建表(m154/155/156/157) + 种子行 → TestClient 走裸 SQL 查询路径。
覆盖: overview 计数/最新日期 → quote-snapshots 升序+空态+400 → auction/chip 按
date+symbol 过滤 → dragon-tiger date/symbol 两种取法 + 缺参 400 → Redis 不可用
统计全 None(不伪装)。
"""
from __future__ import annotations

import src.web.database as webdb
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web.api import market_archive
from src.web.migrations import (
    _m154_auction_snapshots_table,
    _m155_chip_daily_table,
    _m156_dragon_tiger_events_table,
    _m157_quote_snapshots_table,
)


def _mk_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m154_auction_snapshots_table(conn)
        _m155_chip_daily_table(conn)
        _m156_dragon_tiger_events_table(conn)
        _m157_quote_snapshots_table(conn)
    return eng


def _seed(eng):
    with eng.begin() as conn:
        for ts, sym in (("2026-09-10 09:31:00", "600000"), ("2026-09-10 09:32:00", "600000")):
            conn.execute(
                text(
                    "INSERT INTO quote_snapshots (trade_date, ts, symbol, market, name, last, source)"
                    " VALUES ('20260910', :ts, :sym, 'CN', '浦发', 10.0, 'tencent')"
                ),
                {"ts": ts, "sym": sym},
            )
        conn.execute(
            text(
                "INSERT INTO quote_snapshots (trade_date, ts, symbol, market, name, last, source)"
                " VALUES ('20260910', '2026-09-10 09:31:00', 'sh000001', 'IDX', '上证指数', 3934.4, 'tencent')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO auction_snapshots (trade_date, symbol, market, source, auction_price)"
                " VALUES ('20260910', '002361', 'CN', 'tq+thsdk', 10.1)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO auction_snapshots (trade_date, symbol, market, source, auction_price)"
                " VALUES ('20260910', '601991', 'CN', 'tq+thsdk', 6.0)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO chip_daily (trade_date, symbol, market, cost_50, profit_ratio)"
                " VALUES ('20260910', '002361', 'CN', 9.8, 0.12)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO dragon_tiger_events (trade_date, symbol, name, reason, close, change_pct, source)"
                " VALUES ('20260909', '603843', '正平股份', '日涨幅偏离', 10.0, 10.0, 'eastmoney')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO dragon_tiger_events (trade_date, symbol, name, reason, close, change_pct, source)"
                " VALUES ('20260910', '603843', '正平股份', '换手率达20%', 11.0, 10.0, 'eastmoney')"
            )
        )


def _client(monkeypatch):
    eng = _mk_engine()
    _seed(eng)
    Sess = sessionmaker(bind=eng)
    monkeypatch.setattr(webdb, "SessionLocal", Sess)
    app = FastAPI()
    app.include_router(market_archive.router, prefix="/api/archive")
    return TestClient(app), eng


def test_overview_freshness(monkeypatch):
    client, _ = _client(monkeypatch)
    r = client.get("/api/archive/overview")
    assert r.status_code == 200
    data = r.json()
    by_name = {t["table"]: t for t in data["tables"]}
    assert len(data["tables"]) == 6
    assert by_name["quote_snapshots"]["rows"] == 3
    assert by_name["quote_snapshots"]["latest_date"] == "20260910"
    assert by_name["dragon_tiger_events"]["rows"] == 2


def test_quote_snapshots_route(monkeypatch):
    client, _ = _client(monkeypatch)
    r = client.get("/api/archive/quote-snapshots", params={"symbol": "600000", "date": "20260910"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    assert [i["ts"] for i in data["items"]] == ["2026-09-10 09:31:00", "2026-09-10 09:32:00"]
    # 指数走 IDX market, 默认 CN 查不到
    r2 = client.get("/api/archive/quote-snapshots", params={"symbol": "sh000001", "date": "20260910"})
    assert r2.json()["count"] == 0
    r3 = client.get("/api/archive/quote-snapshots", params={"symbol": "sh000001", "date": "20260910", "market": "IDX"})
    assert r3.json()["count"] == 1
    # 边界: 空 symbol / 坏日期
    assert client.get("/api/archive/quote-snapshots", params={"symbol": " ", "date": "20260910"}).status_code == 400
    assert client.get("/api/archive/quote-snapshots", params={"symbol": "600000", "date": "2026-9-1"}).status_code == 400


def test_auction_route(monkeypatch):
    client, _ = _client(monkeypatch)
    r = client.get("/api/archive/auction-snapshots", params={"date": "20260910"})
    assert r.status_code == 200 and r.json()["count"] == 2
    r2 = client.get("/api/archive/auction-snapshots", params={"date": "20260910", "symbol": "002361"})
    assert r2.json()["count"] == 1 and r2.json()["items"][0]["auction_price"] == 10.1
    r3 = client.get("/api/archive/auction-snapshots", params={"date": "20260101"})
    assert r3.json() == {"date": "20260101", "count": 0, "note": "竞价快照当日唯一; 空=当日未采集", "items": []}
    # date 缺省 → 默认今天(合法), 只校验坏日期 400
    assert client.get("/api/archive/auction-snapshots", params={"date": "bad"}).status_code == 400


def test_chip_route(monkeypatch):
    client, _ = _client(monkeypatch)
    r = client.get("/api/archive/chip-daily", params={"date": "20260910", "symbol": "002361"})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1 and items[0]["cost_50"] == 9.8


def test_dragon_tiger_route(monkeypatch):
    client, _ = _client(monkeypatch)
    # 按日全榜
    r = client.get("/api/archive/dragon-tiger", params={"date": "20260910"})
    assert r.status_code == 200 and r.json()["count"] == 1
    # 按股近 7 日
    r2 = client.get("/api/archive/dragon-tiger", params={"symbol": "603843", "days": 7})
    assert r2.json()["count"] == 2
    # 日期降序在前
    assert [i["trade_date"] for i in r2.json()["items"]] == ["20260910", "20260909"]
    # 缺参 → 400
    assert client.get("/api/archive/dragon-tiger").status_code == 400


def test_redis_stats_none_without_url(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    stats = market_archive._redis_stats()
    assert stats == {"keyspace_hits": None, "keyspace_misses": None, "hit_rate": None}
