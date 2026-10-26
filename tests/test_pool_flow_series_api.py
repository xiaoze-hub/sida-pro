# -*- coding: utf-8 -*-
"""暗盘资金副图序列端点契约(2026-10-11 决策先锋副图1 §2)。

`GET /api/klines/{symbol}/pool-flow-series` 供前端 K 线「暗盘资金」档红绿柱取数。
契约(全 mock, 禁真实网络):
- CN 命中计算 → available:true + series 原样透传 + confidence='L1_approx'(诚实近似标注);
- 无 K 线(计算返回空) → available:false + 显式 note(不返 0 冒充);
- 计算抛异常 → available:false + 显式 note(不 500);
- 非 CN → 结构化降级 available:false(明/暗盘为 A 股口径)。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _client() -> TestClient:
    from src.web.api import klines as klines_api

    app = FastAPI()
    app.include_router(klines_api.router, prefix="/api/klines")
    return TestClient(app)


def _patch_cache(monkeypatch):
    from src.web.api import klines as klines_api

    monkeypatch.setattr(klines_api.biz_cache, "get_json", lambda key: None)
    monkeypatch.setattr(klines_api.biz_cache, "set_json", lambda key, value, ttl=None: True)


def test_pool_flow_series_ok(monkeypatch):
    _patch_cache(monkeypatch)
    from src.core import dark_pool_flow as dpf

    monkeypatch.setattr(dpf, "compute_pool_flow_series", lambda symbol, days=5, today_overlay=True: {
        "symbol": symbol, "days": days,
        "series": [
            {"date": "2026-10-08", "main_net": 1234567.0, "approximation": True, "source": "ohlc_dark_alloc"},
            {"date": "2026-10-09", "main_net": -234567.0, "approximation": False, "source": "ming+dark_authoritative"},
        ],
        "total_net": 1000000.0, "today_authoritative": -234567.0,
        "note": "历史日=暗盘OHLC近似(方向可靠/幅度有偏差); 当日已叠加权威ming+dark",
    })
    body = _client().get("/api/klines/600111/pool-flow-series?days=10").json()
    assert body["available"] is True and body["confidence"] == "L1_approx"
    assert body["days"] == 10 and len(body["series"]) == 2
    assert body["series"][0]["approximation"] is True


def test_pool_flow_series_empty_no_fabrication(monkeypatch):
    _patch_cache(monkeypatch)
    from src.core import dark_pool_flow as dpf

    monkeypatch.setattr(dpf, "compute_pool_flow_series",
                        lambda symbol, days=5, today_overlay=True: {
                            "symbol": symbol, "days": days, "series": [],
                            "total_net": None, "today_authoritative": None, "note": "无数据"})
    body = _client().get("/api/klines/600111/pool-flow-series").json()
    assert body["available"] is False and body["series"] == []
    assert body["note"] == "无数据"


def test_pool_flow_series_error_degrades(monkeypatch):
    _patch_cache(monkeypatch)
    from src.core import dark_pool_flow as dpf

    def _boom(symbol, days=5, today_overlay=True):
        raise RuntimeError("thsdk down")

    monkeypatch.setattr(dpf, "compute_pool_flow_series", _boom)
    resp = _client().get("/api/klines/600111/pool-flow-series")
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False and "无数据" in body["note"]


def test_pool_flow_series_non_cn_degrade(monkeypatch):
    _patch_cache(monkeypatch)
    body = _client().get("/api/klines/00700/pool-flow-series?market=HK").json()
    assert body["available"] is False and body["confidence"] is None
    assert "仅 A 股" in body["note"]
