# -*- coding: utf-8 -*-
"""I-8 分价矩阵 API 测试(2026-10-18): 路由挂载 + 三种出参 + 降级诚实 + 归档闭环。

不触网: 读穿透层(`get_or_refresh`)被 monkeypatch, 归档/历史用临时 SQLite。
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.db.models  # noqa: F401
from src.core.price_distribution_store import archive_daily
from src.db.session import Base
from src.web.api import price_distribution as api


def _analysis(**over):
    base = {
        "available": True,
        "symbol": "002361",
        "market": "CN",
        "trade_date": "2026-10-16",
        "snapshot_ts": "15:00:02",
        "quote": {"close": 9.06},
        "day": {"total_vol": 67000.0, "buy_vol": 39699.0, "net_amount": 12398.0,
                "buy_ratio": 0.5925, "big_ratio": 0.006},
        "bins": [{"price": 9.05}],
        "support_levels": [{"price": 9.05, "buy_ratio": 0.78}],
        "resistance_levels": [{"price": 9.30, "buy_ratio": 0.2}],
        "iceberg_signals": [],
        "summary": [{"key": "support", "text": "9.05 (主动买占比 78.0%)"}],
        "quality": {"bins": 1},
        "cache": {"hit": False, "source": "live", "stale": False},
    }
    base.update(over)
    return base


@pytest.fixture()
def session_factory(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/api.db")
    Base.metadata.create_all(bind=eng)
    yield sessionmaker(bind=eng)
    eng.dispose()


def test_routes_are_mounted_at_api_market_prefix():
    from src.web.app import app

    paths = app.openapi()["paths"]
    assert "/api/market/price-distribution/{symbol}" in paths
    assert "/api/market/price-distribution/{symbol}/history" in paths
    assert "/api/market/price-distribution/{symbol}/archive" in paths


def test_handler_returns_matrix_with_cache_meta(monkeypatch):
    monkeypatch.setattr(api, "get_or_refresh", lambda *a, **kw: _analysis())

    res = asyncio.run(api.get_price_distribution("002361", market="CN", limit=70,
                                                 refresh=False, persist=True, ttl_s=None))
    assert res["available"] is True
    assert res["symbol"] == "002361"
    assert res["support_levels"][0]["price"] == pytest.approx(9.05)
    assert res["cache"]["source"] == "live"


def test_handler_passes_ttl_and_refresh_through(monkeypatch):
    seen = {}

    def fake(sym, market, **kw):
        seen.update({"sym": sym, "market": market, **kw})
        return _analysis()

    monkeypatch.setattr(api, "get_or_refresh", fake)
    asyncio.run(api.get_price_distribution("002361", market="CN", limit=30,
                                           refresh=True, persist=False, ttl_s=5))
    assert seen["sym"] == "002361"
    assert seen["limit"] == 30
    assert seen["refresh"] is True
    assert seen["persist"] is False
    assert seen["ttl_s"] == 5


def test_handler_degrades_on_blank_symbol():
    res = asyncio.run(api.get_price_distribution("  ", market="CN", limit=70,
                                                 refresh=False, persist=True, ttl_s=None))
    assert res["available"] is False
    assert res["reason"] == "缺少标的代码"


def test_handler_degrades_instead_of_raising(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("store 炸了")

    monkeypatch.setattr(api, "get_or_refresh", boom)
    res = asyncio.run(api.get_price_distribution("002361", market="CN", limit=70,
                                                 refresh=False, persist=True, ttl_s=None))
    assert res["available"] is False
    assert "store 炸了" in res["reason"]


def test_history_reads_archive_only(monkeypatch, session_factory):
    with session_factory() as s:
        archive_daily(s, _analysis(trade_date="2026-10-15"), settled_at=1.0)
        archive_daily(s, _analysis(trade_date="2026-10-16"), settled_at=2.0)
    monkeypatch.setattr(api, "SessionLocal", session_factory)

    res = asyncio.run(api.get_price_distribution_history("002361", market="CN", days=60))
    assert res["available"] is True
    assert res["count"] == 2
    assert [i["trade_date"] for i in res["items"]] == ["2026-10-15", "2026-10-16"]
    assert res["items"][-1]["support_price"] == pytest.approx(9.05)


def test_history_empty_is_explicit(monkeypatch, session_factory):
    monkeypatch.setattr(api, "SessionLocal", session_factory)
    res = asyncio.run(api.get_price_distribution_history("002361", market="CN", days=60))
    assert res["available"] is False
    assert "尚无分价日终归档" in res["reason"]
    assert res["items"] == []


def test_archive_writes_daily_row(monkeypatch, session_factory):
    monkeypatch.setattr(api, "get_or_refresh", lambda *a, **kw: _analysis())
    monkeypatch.setattr(api, "SessionLocal", session_factory)

    res = asyncio.run(api.post_price_distribution_archive("002361", market="CN",
                                                          limit=70, refresh=True))
    assert res["archived"] is True
    assert res["trade_date"] == "2026-10-16"
    assert res["support_price"] == pytest.approx(9.05)
    assert res["resistance_price"] == pytest.approx(9.30)

    hist = asyncio.run(api.get_price_distribution_history("002361", market="CN", days=10))
    assert hist["count"] == 1


def test_archive_skips_when_source_unavailable(monkeypatch, session_factory):
    monkeypatch.setattr(
        api, "get_or_refresh",
        lambda *a, **kw: {"available": False, "reason": "腾讯分价表无数据", "trade_date": None},
    )
    monkeypatch.setattr(api, "SessionLocal", session_factory)

    res = asyncio.run(api.post_price_distribution_archive("002361", market="CN",
                                                          limit=70, refresh=True))
    assert res["archived"] is False
    assert "腾讯分价表无数据" in res["reason"]
