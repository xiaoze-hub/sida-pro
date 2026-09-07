"""方向1回归: 来源透传 + vendor trust 打分。"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.api import datasources as ds_api
from src.web.api.quotes import _quote_to_response
from src.models.market import MarketCode


def test_quote_response_carries_source():
    full = _quote_to_response(
        "600519", MarketCode("CN"),
        {"symbol": "600519", "current_price": 1500.0, "source": "tencent",
         "source_latency_ms": 320, "quote_date": "2026-09-08"},
    )
    assert full["source"] == "tencent" and full["source_latency_ms"] == 320
    empty = _quote_to_response("600519", MarketCode("CN"), None)
    assert empty["source"] == "" and empty["source_latency_ms"] == 0
    assert empty["current_price"] is None  # 无数据不编造


def test_trust_endpoint_shape_and_scoring(monkeypatch):
    import src.core.marketdata_client as md_client

    class _FakeMD:
        def health(self):
            return {
                "tencent": {"success_rate": 0.98, "p50_latency_ms": 400,
                            "count": 100, "last_error": ""},
                "eastmoney": {"success_rate": 0.5, "p50_latency_ms": 3000,
                              "count": 20, "last_error": "timeout"},
                "never": {"success_rate": None, "p50_latency_ms": None,
                          "count": 0, "last_error": ""},
            }

    monkeypatch.setattr(md_client, "get_market_data", lambda: _FakeMD())
    app = FastAPI()
    app.include_router(ds_api.router, prefix="/ds")
    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/ds/trust")
    assert r.status_code == 200, r.text
    items = {i["vendor"]: i for i in r.json()["items"]}
    assert items["tencent"]["score"] == 98  # 98-0
    assert items["eastmoney"]["score"] == 25  # 50-25
    assert items["never"]["score"] is None  # 没调用过不冒充
    assert r.json()["items"][0]["vendor"] == "tencent"  # 高分在前
