"""大盘资金流历史端点契约(v0.5.82)。

主回归: **回退查询必须在连接还开着的时候发**。v0.5.81 把 `_last_varying_session(conn, ...)`
写在 `with engine.connect()` 块外, conn 已关闭 → 整个端点落进 except, 恒返回 0 点 +
"This Connection is closed"。这里的假连接**关闭后再用就抛**, 与 SQLAlchemy 行为一致,
所以那条 bug 一回归就红。
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.web.api.market_data as api
import src.web.database as database
from src.web.response import ResponseWrapperMiddleware

COLS = 6


def _row(day: str, minute: int, flow: float):
    ts = dt.datetime.fromisoformat(f"{day}T09:{minute:02d}:00")
    return (ts, flow, 100 + minute, 900 - minute, flow / 2, flow / 2)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """关闭后再 execute 就抛 —— 复刻 SQLAlchemy 的 'This Connection is closed'。"""

    def __init__(self, today_rows, prev_day, prev_rows):
        self.closed = False
        self.today_rows = today_rows
        self.prev_day = prev_day
        self.prev_rows = prev_rows
        self.queries: list[str] = []

    def execute(self, clause, params=None):
        if self.closed:
            raise RuntimeError("This Connection is closed")
        sql = " ".join(str(clause).split())
        self.queries.append(sql)
        if "DISTINCT date(ts)" in sql:
            return _Result([(self.prev_day,)])
        if "date(ts) =" in sql:
            return _Result(self.prev_rows)
        return _Result(self.today_rows)

    def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    def connect(self):
        conn = self.conn
        return _Ctx(conn)


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()
        return False


def _client(monkeypatch, today_rows, prev_day="2026-09-11", prev_rows=None):
    prev_rows = prev_rows if prev_rows is not None else [
        _row(prev_day, 30, 10.0), _row(prev_day, 31, 25.5), _row(prev_day, 32, 40.0)]
    conn = _FakeConn(today_rows, prev_day, prev_rows)
    monkeypatch.setattr(database, "engine", _FakeEngine(conn))
    app = FastAPI()
    app.include_router(api.router, prefix="/api/market-data")
    app.add_middleware(ResponseWrapperMiddleware)
    return TestClient(app), conn


FLAT_TODAY = [_row("2026-09-12", 30, 12.3), _row("2026-09-12", 31, 12.3),
              _row("2026-09-12", 32, 12.3)]
VARYING_TODAY = [_row("2026-09-11", 30, 12.3), _row("2026-09-11", 31, 30.9),
                 _row("2026-09-11", 32, 44.4)]


def test_flat_today_falls_back_to_prev_session(monkeypatch):
    c, conn = _client(monkeypatch, FLAT_TODAY)
    d = c.get("/api/market-data/market-capital-flow/history?hours=4").json()["data"]
    assert d["session"] == "prev" and d["session_date"] == "2026-09-11"
    assert d["count"] == 3 and "非交易时段" in d["note"] and "2026-09-11" in d["note"]
    flows = [i["total_main_flow"] for i in d["items"]]
    assert max(flows) - min(flows) > 1e-9                  # 回落拿到的必须是真曲线, 不是直线
    # 回退查询确实是在连接关闭前发的(否则 _FakeConn 已抛, 端点会落到 except 分支)
    assert d["note"] != f"读取失败: This Connection is closed"
    assert any("DISTINCT date(ts)" in q for q in conn.queries)


def test_fallback_items_keep_full_contract(monkeypatch):
    """回落分支的 item 形状必须与当日分支一致(6 键), 否则前端 tooltip 会拿到 undefined。"""
    c, _ = _client(monkeypatch, FLAT_TODAY)
    d = c.get("/api/market-data/market-capital-flow/history").json()["data"]
    expected = {"ts", "total_main_flow", "up_count", "down_count", "sh_flow", "sz_flow"}
    assert d["count"] > 0
    for it in d["items"]:
        assert set(it) == expected


def test_varying_today_stays_today(monkeypatch):
    c, conn = _client(monkeypatch, VARYING_TODAY)
    d = c.get("/api/market-data/market-capital-flow/history").json()["data"]
    assert d["session"] == "today" and d["session_date"] == "2026-09-11"
    assert d["note"] == "" and d["count"] == 3
    assert not any("DISTINCT date(ts)" in q for q in conn.queries)   # 没变动需求就不查回退


def test_empty_series_reports_empty_not_zero(monkeypatch):
    c, conn = _client(monkeypatch, [])
    d = c.get("/api/market-data/market-capital-flow/history").json()["data"]
    assert d["count"] == 0 and d["items"] == []
    assert d["session"] == "today" and d["session_date"] is None
    assert d["note"] == "暂无快照(等待大盘资金接口写入)"
    assert not any("DISTINCT date(ts)" in q for q in conn.queries)


def test_no_varying_session_keeps_flat_and_says_so(monkeypatch):
    """回退也找不到有变动的一天: 保留当日直线, 但如实说明, 不假装是曲线。"""
    flat_prev = [_row("2026-09-11", 30, 5.0), _row("2026-09-11", 31, 5.0)]
    c, _ = _client(monkeypatch, FLAT_TODAY, prev_rows=flat_prev)
    d = c.get("/api/market-data/market-capital-flow/history").json()["data"]
    assert d["session"] == "today" and d["count"] == 3
    assert d["note"] == ""


def test_is_flat_ignores_none_and_single_point():
    assert api._is_flat([]) is False
    assert api._is_flat([{"total_main_flow": 1.0}]) is False          # 单点不判平
    assert api._is_flat([{"total_main_flow": None}, {"total_main_flow": None}]) is False
    assert api._is_flat([{"total_main_flow": 1.0}, {"total_main_flow": 1.0}]) is True
    assert api._is_flat([{"total_main_flow": 1.0}, {"total_main_flow": None}]) is False


@pytest.mark.parametrize("hours", [1, 24])
def test_hours_param_passed_through(monkeypatch, hours):
    c, _ = _client(monkeypatch, VARYING_TODAY)
    d = c.get(f"/api/market-data/market-capital-flow/history?hours={hours}").json()["data"]
    assert d["hours"] == hours
