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


class _FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
        self.last_params = None

    def execute(self, stmt, params):
        self.last_params = params
        return self

    def fetchall(self):
        return self._rows


class _FakeBegin:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def test_read_ohlc_filters_none_rows_and_short_circuits(monkeypatch):
    rows = [
        _FakeRow({"ts": "2026-09-11 00:00:00+08:00", "symbol": "A", "open": 1, "high": 2,
                  "low": 1, "close": 2, "amount": 1.5e8}),
        _FakeRow({"ts": "2026-09-11 00:00:00+08:00", "symbol": "B", "open": 1, "high": None,
                  "low": 1, "close": 2, "amount": 1e8}),
    ]
    conn = _FakeConn(rows)
    fake_engine = type("E", (), {"begin": lambda self: _FakeBegin(conn)})()
    monkeypatch.setattr("src.db.session.engine", fake_engine)
    out = api._read_ohlc(["20260911"], ["A", "B"])
    assert out == {("20260911", "A"): {"o": 1, "h": 2, "l": 1, "c": 2, "amount": 1.5e8}}  # B 缺 high 被丢
    assert conn.last_params == {"dates": ("2026-09-11",), "codes": ("A", "B")}  # 入参转 ISO
    assert api._read_ohlc([], ["A"]) == {}
    assert api._read_ohlc(["20260911"], []) == {}


def test_ladder_contract_marks_stocks_and_mode(monkeypatch):
    dates = ["20260910", "20260911"]
    ev = [
        {"trade_date": "20260910", "symbol": "C", "name": "CC", "is_sealed_close": True},
        {"trade_date": "20260911", "symbol": "C", "name": "CC", "is_sealed_close": True},
        {"trade_date": "20260911", "symbol": "Z", "name": "ZZ", "is_sealed_close": False},
        {"trade_date": "20260911", "symbol": "D", "name": "DD", "is_sealed_close": True},
    ]

    def fake_read(sql, params):
        if "DISTINCT trade_date" in sql:
            return [{"trade_date": d} for d in reversed(dates)]
        return [e for e in ev if e["trade_date"] >= params["a"]]

    monkeypatch.setattr(api, "_read", fake_read)
    monkeypatch.setattr(api, "_read_ohlc",
                        lambda d, s: {("20260911", "C"): {"o": 1, "h": 2, "l": 1, "c": 2}})
    c = _client(monkeypatch, ROWS)
    r = c.get("/api/theme-mood/ladder?window=20")
    d = r.json()["data"]
    assert d["mode"] == "finalized" and d["stale"] is False and d["degraded"] is None
    assert d["live_day"] is None
    day = {x["date"]: x for x in d["ladder"]}[ "20260911"]
    assert [b["symbol"] for b in day["blown"]] == ["Z"]
    assert day["broken"] == []
    stocks = {s["symbol"]: s for row in day["rows"] for s in row["stocks"]}
    assert stocks["C"]["candle"] == {"o": 1, "h": 2, "l": 1, "c": 2}
    assert stocks["D"]["candle"] is None                      # 缺 OHLC 不编
    one = [row for row in day["rows"] if row["boards"] == 1]
    assert len(one) == 1 and one[0]["tag"] == "首板"           # D 仅当日封板=首板
    assert [row for row in day["rows"] if row["boards"] == 2][0]["tag"] is None
    assert d["dates"] == dates          # 日期仍紧凑, 格式化归前端


def test_ladder_rejects_bad_mode(monkeypatch):
    monkeypatch.setattr(api, "_read", lambda sql, params: [])
    c = _client(monkeypatch, ROWS)
    assert c.get("/api/theme-mood/ladder?mode=bogus").status_code == 400
    # live 已上线(v0.5.87): 无快照时降级 finalized(200), 不再 400
    assert c.get("/api/theme-mood/ladder?mode=live").status_code == 200
    assert c.get("/api/theme-mood/ladder?mode=live").json()["data"]["mode"] == "finalized"


def _ladder_client(monkeypatch, snapshot):
    dates = ["20260910", "20260911"]
    ev = [
        {"trade_date": "20260910", "symbol": "C", "name": "CC", "is_sealed_close": True},
        {"trade_date": "20260911", "symbol": "C", "name": "CC", "is_sealed_close": True},
    ]

    def fake_read(sql, params):
        if "DISTINCT trade_date" in sql:
            return [{"trade_date": d} for d in reversed(dates)]
        return [e for e in ev if e["trade_date"] >= params["a"]]

    monkeypatch.setattr(api, "_read", fake_read)
    monkeypatch.setattr(api, "_read_ohlc", lambda d, s: {})
    monkeypatch.setattr(api, "_live_snapshot", lambda: snapshot)
    return _client(monkeypatch, ROWS)


def test_ladder_live_mode_when_snapshot(monkeypatch):
    snap = {"live_day": {"date": "20260912", "rows": [], "blown": [], "broken": [],
                         "provisional": True},
            "meta": {"stale": False}, "date": "20260912", "closing": False}
    c = _ladder_client(monkeypatch, snap)
    d = c.get("/api/theme-mood/ladder?mode=auto").json()["data"]
    assert d["mode"] == "live"
    assert d["live_day"]["provisional"] is True
    assert d["stale"] is False and d["note_closing"] is None


def test_ladder_stale_and_closing_flags(monkeypatch):
    snap = {"live_day": {"date": "20260912", "rows": [], "blown": [], "broken": [],
                         "provisional": True},
            "meta": {"stale": True}, "date": "20260912", "closing": True}
    c = _ladder_client(monkeypatch, snap)
    d = c.get("/api/theme-mood/ladder?mode=auto").json()["data"]
    assert d["mode"] == "live" and d["stale"] is True
    assert d["note_closing"] == "收盘撮合中, 稍后定型"


def test_ladder_no_snapshot_degrades_finalized(monkeypatch):
    c = _ladder_client(monkeypatch, None)
    d = c.get("/api/theme-mood/ladder?mode=live").json()["data"]  # live 请求但无快照
    assert d["mode"] == "finalized" and d["live_day"] is None


def test_ladder_stats_from_latest_finalized(monkeypatch):
    dates = ["20260910", "20260911"]
    ev = [
        # 0910: C 封(昨候选1)
        {"trade_date": "20260910", "symbol": "C", "name": "CC", "is_sealed_close": True},
        # 0911: C 封(晋级, boards=2), D 首板, Z 触板未封(炸板)
        {"trade_date": "20260911", "symbol": "C", "name": "CC", "is_sealed_close": True},
        {"trade_date": "20260911", "symbol": "D", "name": "DD", "is_sealed_close": True},
        {"trade_date": "20260911", "symbol": "Z", "name": "ZZ", "is_sealed_close": False},
    ]

    def fake_read(sql, params):
        if "DISTINCT trade_date" in sql:
            return [{"trade_date": d} for d in reversed(dates)]
        return [e for e in ev if e["trade_date"] >= params["a"]]

    monkeypatch.setattr(api, "_read", fake_read)
    monkeypatch.setattr(api, "_read_ohlc", lambda d, s: {})
    monkeypatch.setattr(api, "_live_snapshot", lambda: None)
    d = _client(monkeypatch, ROWS).get("/api/theme-mood/ladder?window=20").json()["data"]
    st = d["stats"]
    assert st["prev_candidates"] == 1      # 0910 只有 C
    assert st["first"] == 1                # 0911 D 首板
    assert st["promoted"] == 1             # 0911 C 晋级(2板)
    assert st["blown"] == 1                # Z 炸板
    assert st["broken"] == 0
    assert st["charging"] == 0
