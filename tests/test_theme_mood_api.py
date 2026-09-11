"""题材情绪分 API 契约测试(2026-09-12)。

含全局响应信封(ResponseWrapperMiddleware): 成功 → {code:0, success:true, data},
参数错误 → 400 + 错误信封。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.web.api.theme_mood as api
from src.web.response import ResponseWrapperMiddleware


def _client(monkeypatch, rows, detail=None, latest="20260911"):
    monkeypatch.setattr(api, "_board_rows", lambda window, top: rows)
    monkeypatch.setattr(api, "_latest_date", lambda: latest)
    if detail is not None:
        monkeypatch.setattr(api, "_detail_rows", lambda code, days: detail)
    app = FastAPI()
    app.include_router(api.router, prefix="/api/theme-mood")
    app.add_middleware(ResponseWrapperMiddleware)
    return TestClient(app)


ROWS = [
    {"block_code": "881101.SH", "block_name": "元件", "block_type": "industry", "score": 78.2,
     "delta": 4.1, "confidence": 86, "core": True, "s1": 80.0, "s2": 75.0, "s3": 82.0, "s4": 70.0, "s5": 60.0,
     "limit_up_cnt": 9, "core_stocks": [{"symbol": "600001.SH", "name": "甲"}],
     "cells": [{"date": "20260911", "score": 78.2, "limit_up_cnt": 9}]},
]


def test_board_contract(monkeypatch):
    c = _client(monkeypatch, ROWS)
    r = c.get("/api/theme-mood/board?window=20&top=15")
    assert r.status_code == 200
    j = r.json()
    assert j["success"] is True and j["code"] == 0
    d = j["data"]
    assert d["trade_date"] == "20260911" and len(d["items"]) == 1
    assert d["items"][0]["block_code"] == "881101.SH" and d["items"][0]["core"] is True


def test_board_validates_params(monkeypatch):
    c = _client(monkeypatch, ROWS)
    assert c.get("/api/theme-mood/board?window=99").status_code == 400
    assert c.get("/api/theme-mood/board?top=99").status_code == 400


def test_board_empty_state(monkeypatch):
    c = _client(monkeypatch, [], latest=None)
    d = c.get("/api/theme-mood/board").json()["data"]
    assert d["trade_date"] is None and d["items"] == []


def test_detail_not_found_and_found(monkeypatch):
    c = _client(monkeypatch, ROWS, detail=[])
    assert c.get("/api/theme-mood/detail/880001.SH").status_code == 404
    c2 = _client(monkeypatch, ROWS, detail=[{"trade_date": "20260911", "score": 78.2}])
    j = c2.get("/api/theme-mood/detail/881101.SH").json()
    assert j["data"]["items"][0]["score"] == 78.2


def test_scan_run_returns_started(monkeypatch):
    monkeypatch.setattr(api, "_spawn_scan", lambda: {"started": True, "reason": None})
    c = _client(monkeypatch, ROWS)
    assert c.post("/api/theme-mood/scan/run").json()["data"]["started"] is True
