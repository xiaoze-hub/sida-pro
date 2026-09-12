"""题材情绪分 API 契约测试(2026-09-12)。

含全局响应信封(ResponseWrapperMiddleware): 成功 → {code:0, success:true, data},
参数错误 → 400 + 错误信封。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.web.api.theme_mood as api
from src.web.response import ResponseWrapperMiddleware


def _client(monkeypatch, rows, detail=None, latest="20260911", dates=("20260910", "20260911"), market=None,
            rotation=None, rotation_top_k=10):
    mkt = market if market is not None else [{"date": d, "score": None} for d in dates]
    rot = rotation if rotation is not None else [
        {"date": d, "new_n": 0, "exit_n": 0, "new_codes": [], "exit_codes": []} for d in dates]
    monkeypatch.setattr(api, "_board_data",
                        lambda window, top: {"dates": list(dates), "items": rows, "market": mkt,
                                             "rotation": rot, "rotation_top_k": rotation_top_k})
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
    c = _client(monkeypatch, ROWS, market=[{"date": "20260910", "score": 71.5},
                                           {"date": "20260911", "score": 71.7}])
    r = c.get("/api/theme-mood/board?window=20&top=15")
    assert r.status_code == 200
    j = r.json()
    assert j["success"] is True and j["code"] == 0
    d = j["data"]
    assert d["trade_date"] == "20260911" and len(d["items"]) == 1
    assert d["dates"] == ["20260910", "20260911"]
    assert d["items"][0]["block_code"] == "881101.SH" and d["items"][0]["core"] is True
    # 走势曲线与轴同长同序(前端逐列取点)
    assert d["market"] == [{"date": "20260910", "score": 71.5}, {"date": "20260911", "score": 71.7}]


def test_board_endpoint_forwards_rotation(monkeypatch):
    """端点必须把轮动载荷透出去。

    v0.5.81 走查发现的缺陷: `_board_data` 算好了 rotation, 但 `/board` 的返回字典没带它,
    前端轮动条永远空 —— 只有打端点才测得出来(打 helper 测不出)。
    """
    rot = [{"date": "20260910", "new_n": 2, "exit_n": 1, "new_codes": ["C", "D"], "exit_codes": ["B"]},
           {"date": "20260911", "new_n": 1, "exit_n": 1, "new_codes": ["B"], "exit_codes": ["A"]}]
    c = _client(monkeypatch, ROWS, rotation=rot, rotation_top_k=10)
    d = c.get("/api/theme-mood/board?window=20&top=15").json()["data"]
    assert d["rotation"] == rot
    assert d["rotation_top_k"] == 10


def test_board_validates_params(monkeypatch):
    c = _client(monkeypatch, ROWS)
    assert c.get("/api/theme-mood/board?window=99").status_code == 400
    assert c.get("/api/theme-mood/board?top=99").status_code == 400


def test_board_empty_state(monkeypatch):
    c = _client(monkeypatch, [], latest=None, dates=())
    d = c.get("/api/theme-mood/board").json()["data"]
    assert d["trade_date"] is None and d["items"] == [] and d["dates"] == [] and d["market"] == []


def test_board_data_empty_shape_is_complete(monkeypatch):
    """空表时 `_board_data` 的结构必须与有数据时同形, 否则端点取 rotation 直接 KeyError → 500。"""
    monkeypatch.setattr(api, "_read", lambda sql, params: [])
    assert api._board_data(20, 15) == {
        "dates": [], "items": [], "market": [], "rotation": [],
        "rotation_top_k": api.ROTATION_TOP_K,
    }


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


def _row(code, name, score, d):
    return {"block_code": code, "block_name": name, "block_type": "industry", "score": score,
            "delta": None, "confidence": 80, "core": False, "s1": score, "s2": score, "s3": score,
            "s4": score, "s5": score, "limit_up_cnt": 3, "max_boards": 2, "core_stocks": None,
            "trade_date": d}


def test_board_rows_include_rotated_out_themes(monkeypatch):
    """退榜题材必须留在行集合里(score=None, in_top_today=False), 轮动序列如实给出进出。"""
    dates = ["20260909", "20260910", "20260911"]
    hist = [
        {"block_code": "A", "trade_date": "20260909", "score": 90.0, "limit_up_cnt": 5},
        {"block_code": "B", "trade_date": "20260909", "score": 80.0, "limit_up_cnt": 3},
        {"block_code": "A", "trade_date": "20260910", "score": 85.0, "limit_up_cnt": 4},
        {"block_code": "C", "trade_date": "20260910", "score": 95.0, "limit_up_cnt": 6},
        {"block_code": "C", "trade_date": "20260911", "score": 90.0, "limit_up_cnt": 5},
        {"block_code": "B", "trade_date": "20260911", "score": 60.0, "limit_up_cnt": 2},
    ]

    def fake_read(sql, params):
        if "DISTINCT trade_date" in sql:
            return [{"trade_date": d} for d in reversed(dates)]
        if "MAX(trade_date)" in sql:
            return [_row("A", "甲板块", None, "20260910")]
        if sql.strip().startswith("SELECT block_code, score"):
            return [{"block_code": h["block_code"], "score": h["score"]}
                    for h in hist if h["trade_date"] == params["d"]]
        if "block_code, trade_date, score" in sql:
            return hist
        return [_row(h["block_code"], h["block_code"] + "板块", h["score"], h["trade_date"])
                for h in hist if h["trade_date"] == params["d"]]

    monkeypatch.setattr(api, "_read", fake_read)
    monkeypatch.setattr(api, "_read_codes",
                        lambda sql, params, codes: [r for r in fake_read(sql, params)
                                                    if r["block_code"] in codes])
    out = api._board_data(20, 15)
    by = {i["block_code"]: i for i in out["items"]}
    assert set(by) == {"A", "B", "C"}                    # 退榜的 A 仍在行集合里
    a = by["A"]
    assert a["score"] is None and a["in_top_today"] is False
    assert a["top_days"] == 2 and a["last_top_date"] == "20260910"
    # align_cells 会把缺失日补成空格子, 所以数"有分数的格子"
    assert sum(1 for c in a["cells"] if c["score"] is not None) == 2
    rot = {r["date"]: r for r in out["rotation"]}
    assert rot["20260909"]["new_n"] == 0 and rot["20260909"]["exit_n"] == 0   # 首日不猜
    # day2 top2 = C,A → B 退; day3 top2 = C,B → B 回、A 退
    assert rot["20260910"]["new_codes"] == ["C"] and rot["20260910"]["exit_codes"] == ["B"]
    assert rot["20260911"]["new_codes"] == ["B"] and rot["20260911"]["exit_codes"] == ["A"]
    assert out["rotation_top_k"] == api.ROTATION_TOP_K
