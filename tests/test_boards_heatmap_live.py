"""板块热力图实时模式 /api/boards/heatmap?live=... (2026-09-10 盘中实时化)。

口径: live=auto 仅 A股交易时段(交易日历∩时段)内走 thsdk "扩展" 批量实时快照,
非时段/拉取失败回落日线; 实时仅覆盖 涨跌幅/资金净流入/量比/涨速, 面积量能仍用日线成交额;
60s 服务端缓存 + 单飞。响应带 live/as_of 标注(前端据此展示"实时/数据截至")。
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.web.api.boards as boards_api
from src.web import models as M  # noqa: F401
from src.web.database import Base, get_db
from src.web.models import Board, BoardDaily


def _client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(boards_api.router, prefix="/api/boards")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app), Session


def _seed(Session):
    db = Session()
    db.add(Board(block_code="URFI0001", name="半导体", board_type="industry"))
    db.add(Board(block_code="URFI0002", name="白酒", board_type="industry"))
    db.add(BoardDaily(block_code="URFI0001", date=date(2026, 9, 10), change_pct=1.0, fund_net=100.0, volume=1000.0))
    db.add(BoardDaily(block_code="URFI0002", date=date(2026, 9, 10), change_pct=-0.5, fund_net=-50.0, volume=2000.0))
    db.commit()
    db.close()


@pytest.fixture(autouse=True)
def _clear_live_cache():
    boards_api._clear_live_cache()
    yield
    boards_api._clear_live_cache()


def test_auto_outside_hours_uses_daily_no_live_call(monkeypatch):
    client, Session = _client()
    _seed(Session)
    monkeypatch.setattr(boards_api, "in_trading_window", lambda: False)
    calls = {"n": 0}

    def _boom(*a, **k):
        calls["n"] += 1
        return {}

    monkeypatch.setattr(boards_api, "fetch_block_snapshots", _boom)

    body = client.get("/api/boards/heatmap?source=ths&type=industry").json()
    assert calls["n"] == 0
    assert body["live"] is False and body["as_of"] is None
    sem = next(i for i in body["items"] if i["block_code"] == "URFI0001")
    assert sem["change_pct"] == 1.0 and sem["has_daily"] is True
    assert sem.get("volume_ratio") is None


def test_auto_in_hours_merges_live_snapshot(monkeypatch):
    client, Session = _client()
    _seed(Session)
    monkeypatch.setattr(boards_api, "in_trading_window", lambda: True)
    monkeypatch.setattr(
        boards_api,
        "fetch_block_snapshots",
        lambda codes, **k: {
            "URFI0001": {"change_pct": 3.2, "fund_net": 888.0, "volume_ratio": 2.4, "speed": 0.7},
        },
    )

    body = client.get("/api/boards/heatmap?source=ths&type=industry").json()
    assert body["live"] is True and body["as_of"]
    sem = next(i for i in body["items"] if i["block_code"] == "URFI0001")
    assert sem["change_pct"] == 3.2           # 实时覆盖
    assert sem["fund_net"] == 888.0
    assert sem["volume"] == 1000.0            # 面积量能仍用日线成交额
    assert sem["volume_ratio"] == 2.4 and sem["speed"] == 0.7
    assert sem["live"] is True
    # 无实时数据(未返回)的板块保持日线值
    wine = next(i for i in body["items"] if i["block_code"] == "URFI0002")
    assert wine["change_pct"] == -0.5 and wine["live"] is False


def test_live_1_forces_outside_hours(monkeypatch):
    client, Session = _client()
    _seed(Session)
    monkeypatch.setattr(boards_api, "in_trading_window", lambda: False)
    monkeypatch.setattr(
        boards_api, "fetch_block_snapshots",
        lambda codes, **k: {"URFI0001": {"change_pct": 4.4, "fund_net": None, "volume_ratio": None, "speed": None}},
    )
    body = client.get("/api/boards/heatmap?source=ths&type=industry&live=1").json()
    assert body["live"] is True
    sem = next(i for i in body["items"] if i["block_code"] == "URFI0001")
    assert sem["change_pct"] == 4.4
    assert sem["fund_net"] == 100.0  # 实时缺该字段 → 保留日线值


def test_live_0_never_calls_even_in_hours(monkeypatch):
    client, Session = _client()
    _seed(Session)
    monkeypatch.setattr(boards_api, "in_trading_window", lambda: True)
    calls = {"n": 0}
    monkeypatch.setattr(
        boards_api, "fetch_block_snapshots",
        lambda codes, **k: (calls.__setitem__("n", calls["n"] + 1), {})[1],
    )
    body = client.get("/api/boards/heatmap?source=ths&type=industry&live=0").json()
    assert calls["n"] == 0 and body["live"] is False


def test_live_fetch_failure_falls_back_to_daily(monkeypatch):
    client, Session = _client()
    _seed(Session)
    monkeypatch.setattr(boards_api, "in_trading_window", lambda: True)

    def _boom(codes, **k):
        raise RuntimeError("thsdk down")

    monkeypatch.setattr(boards_api, "fetch_block_snapshots", _boom)
    body = client.get("/api/boards/heatmap?source=ths&type=industry").json()
    assert body["live"] is False
    sem = next(i for i in body["items"] if i["block_code"] == "URFI0001")
    assert sem["change_pct"] == 1.0  # 回落日线, 不报错


def test_live_cache_single_flight_within_ttl(monkeypatch):
    client, Session = _client()
    _seed(Session)
    monkeypatch.setattr(boards_api, "in_trading_window", lambda: True)
    calls = {"n": 0}

    def _snap(codes, **k):
        calls["n"] += 1
        return {"URFI0001": {"change_pct": 1.1, "fund_net": None, "volume_ratio": None, "speed": None}}

    monkeypatch.setattr(boards_api, "fetch_block_snapshots", _snap)
    client.get("/api/boards/heatmap?source=ths&type=industry")
    client.get("/api/boards/heatmap?source=ths&type=industry")
    assert calls["n"] == 1  # 60s TTL 内只拉一次
    boards_api._clear_live_cache()
    client.get("/api/boards/heatmap?source=ths&type=industry")
    assert calls["n"] == 2


def test_live_bad_param_400():
    client, Session = _client()
    _seed(Session)
    assert client.get("/api/boards/heatmap?source=ths&type=industry&live=maybe").status_code == 400
