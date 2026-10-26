# -*- coding: utf-8 -*-
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from src.web.api import tdx as tdx_api
from src.web.api.auth import get_current_user

app = FastAPI()
app.include_router(tdx_api.router, prefix="/api/tdx")
app.dependency_overrides[get_current_user] = lambda: {"username": "admin", "role": "admin"}
client = TestClient(app)


def test_tdx_etf_track_mocked():
    mock_etfs = [
        {
            "code": "510300.SH",
            "name": "300ETF华泰柏瑞",
            "now_price": 4.25,
            "iopv": 4.20,
            "premium_rate": 1.19,
            "pre_close": 4.24,
            "market_cap_wan": 1000.0,
            "total_shares_wan": 25000.0,
        },
        {
            "code": "510330.SH",
            "name": "300ETF华夏",
            "now_price": 4.45,
            "iopv": 4.40,
            "premium_rate": 1.14,
            "pre_close": 4.42,
            "market_cap_wan": 500.0,
            "total_shares_wan": 12000.0,
        },
    ]
    with patch("marketdata.vendors.tq.trackzs_etf_info", return_value=mock_etfs):
        resp = client.get("/api/tdx/etf-track?zs_code=000300.SH")
        assert resp.status_code == 200
        data = resp.json()
        assert data["zs_code"] == "000300.SH"
        assert data["total"] == 2
        assert data["premium_count"] == 2
        assert "护盘" in data["status"]
        assert len(data["etfs"]) == 2
