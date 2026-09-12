"""大盘资金流历史端点契约(v0.5.82 连接生命周期 / v0.5.84 回落选日与 note 口径)。

主回归一: **回退查询必须在连接还开着的时候发**。v0.5.81 把 `_last_varying_session(conn, ...)`
写在 `with engine.connect()` 块外, conn 已关闭 → 整个端点落进 except, 恒返回 0 点 +
"This Connection is closed"。这里的假连接**关闭后再用就抛**, 与 SQLAlchemy 行为一致,
所以那条 bug 一回归就红。

主回归二: **回落不排除当天**。原实现把"窗口首条的日期"排除掉, 于是交易日晚上(窗口平、
但当天全天有波动)会跳到昨天去。现在逐日自己判平, 当天有波动就给当天(session=today_full)。
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.web.api.market_data as api
import src.web.database as database
from src.web.response import ResponseWrapperMiddleware

TODAY = dt.datetime.now().strftime("%Y-%m-%d")
PREV = (dt.datetime.now() - dt.timedelta(days=1)).strftime("%Y-%m-%d")


def _row(day: str, minute: int, flow: float):
    ts = dt.datetime.fromisoformat(f"{day}T09:{minute:02d}:00")
    return (ts, flow, 100 + minute, 900 - minute, flow / 2, flow / 2)


def _flat(day: str, flow: float = 12.3):
    return [_row(day, 30, flow), _row(day, 31, flow), _row(day, 32, flow)]


def _varying(day: str):
    return [_row(day, 30, 10.0), _row(day, 31, 25.5), _row(day, 32, 40.0)]


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """关闭后再 execute 就抛 —— 复刻 SQLAlchemy 的 'This Connection is closed'。

    days: [(date_str, rows), ...] 按 DESC 排列, 模拟回溯期内的每日全天序列。
    """

    def __init__(self, window_rows, days):
        self.closed = False
        self.window_rows = window_rows
        self.days = dict(days)
        self.day_order = [d for d, _ in days]
        self.queries: list[str] = []

    def execute(self, clause, params=None):
        if self.closed:
            raise RuntimeError("This Connection is closed")
        sql = " ".join(str(clause).split())
        self.queries.append(sql)
        if "DISTINCT date(ts)" in sql:
            return _Result([(d,) for d in self.day_order])
        if "date(ts) =" in sql:
            return _Result(self.days.get(str((params or {}).get("d")), []))
        return _Result(self.window_rows)

    def close(self):
        self.closed = True


class _Ctx:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        self.conn.close()
        return False


class _FakeEngine:
    def __init__(self, conn):
        self.conn = conn

    def connect(self):
        return _Ctx(self.conn)


def _client(monkeypatch, window_rows, days):
    conn = _FakeConn(window_rows, days)
    monkeypatch.setattr(database, "engine", _FakeEngine(conn))
    app = FastAPI()
    app.include_router(api.router, prefix="/api/market-data")
    app.add_middleware(ResponseWrapperMiddleware)
    return TestClient(app), conn


def _get(c, hours=4):
    return c.get(f"/api/market-data/market-capital-flow/history?hours={hours}").json()["data"]


def test_flat_window_falls_back_to_most_recent_varying_day(monkeypatch):
    """周末: 窗口平、当天全天也平 → 回落到最近一个真有波动的那天, 并如实标注。"""
    c, conn = _client(monkeypatch, _flat(TODAY), [(TODAY, _flat(TODAY)), (PREV, _varying(PREV))])
    d = _get(c)
    assert d["session"] == "prev" and d["session_date"] == PREV
    assert d["count"] == 3 and PREV in d["note"] and "无变动" in d["note"]
    flows = [i["total_main_flow"] for i in d["items"]]
    assert max(flows) - min(flows) > 1e-9                  # 回落拿到的必须是真曲线, 不是直线
    assert any("DISTINCT date(ts)" in q for q in conn.queries)


def test_trading_day_evening_returns_today_not_yesterday(monkeypatch):
    """交易日晚上: 窗口(收盘后)是平的, 但**当天全天有波动** → 必须给当天, 不许跳到昨天。

    用固定时钟(2026-09-11 周五 20:00), 不依赖跑测试的真实时刻 —— 否则跨午夜跑全量会假红
    (模块级 TODAY 与端点内 now() 不同日)。
    """
    fixed_now = dt.datetime(2026, 9, 11, 20, 0, 0)

    class _FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG006
            return fixed_now

    monkeypatch.setattr(api, "datetime", _FixedDateTime)
    day, prev = "2026-09-11", "2026-09-10"
    c, _ = _client(monkeypatch, _flat(day), [(day, _varying(day)), (prev, _varying(prev))])
    d = _get(c)
    assert d["session"] == "today_full" and d["session_date"] == day
    assert day in d["note"] and "全天" in d["note"]
    flows = [i["total_main_flow"] for i in d["items"]]
    assert max(flows) - min(flows) > 1e-9


def test_fallback_items_keep_full_contract(monkeypatch):
    """回落分支的 item 形状必须与当日分支一致(6 键), 否则前端 tooltip 会拿到 undefined。"""
    c, _ = _client(monkeypatch, _flat(TODAY), [(TODAY, _flat(TODAY)), (PREV, _varying(PREV))])
    d = _get(c)
    expected = {"ts", "total_main_flow", "up_count", "down_count", "sh_flow", "sz_flow"}
    assert d["count"] > 0
    for it in d["items"]:
        assert set(it) == expected


def test_varying_window_stays_as_requested(monkeypatch):
    c, conn = _client(monkeypatch, _varying(TODAY), [(TODAY, _varying(TODAY))])
    d = _get(c)
    assert d["session"] == "today" and d["session_date"] == TODAY
    assert d["note"] == "" and d["count"] == 3
    assert not any("DISTINCT date(ts)" in q for q in conn.queries)   # 窗口有波动就不查回落


def test_empty_series_reports_empty_not_zero(monkeypatch):
    c, conn = _client(monkeypatch, [], [(TODAY, [])])
    d = _get(c)
    assert d["count"] == 0 and d["items"] == []
    assert d["session"] == "today" and d["session_date"] is None
    assert d["note"] == "暂无快照(等待大盘资金接口写入)"
    assert not any("DISTINCT date(ts)" in q for q in conn.queries)


def test_no_varying_day_keeps_flat_series(monkeypatch):
    """回溯期内一天都没有波动: 保留窗口原样(直线), 不编造曲线、也不假装回落成功。"""
    c, _ = _client(monkeypatch, _flat(TODAY), [(TODAY, _flat(TODAY)), (PREV, _flat(PREV, 5.0))])
    d = _get(c)
    assert d["session"] == "today" and d["count"] == 3 and d["note"] == ""


def test_is_flat_ignores_none_and_single_point():
    assert api._is_flat([]) is False
    assert api._is_flat([{"total_main_flow": 1.0}]) is False          # 单点不判平
    assert api._is_flat([{"total_main_flow": None}, {"total_main_flow": None}]) is False
    assert api._is_flat([{"total_main_flow": 1.0}, {"total_main_flow": 1.0}]) is True
    assert api._is_flat([{"total_main_flow": 1.0}, {"total_main_flow": None}]) is False


@pytest.mark.parametrize("hours", [1, 24])
def test_hours_param_passed_through(monkeypatch, hours):
    c, _ = _client(monkeypatch, _varying(TODAY), [(TODAY, _varying(TODAY))])
    assert _get(c, hours)["hours"] == hours
