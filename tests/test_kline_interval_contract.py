# -*- coding: utf-8 -*-
"""K 线周期契约(2026-11-07 复审): 月线必须真聚合, 分钟线必须显式拒绝, 绝不伪标。

事故口径:
  · `_aggregate_klines` 只认 "1m"/"m", 不认前端实际发送的 "1mth" → **月K 回传日K**;
  · "1m" 同时被当成"月", 而 A 股习惯里 1m = 1 分钟 → 语义撞车;
  · 5m/15m/30m/60m/intra 不在白名单 → 原样返回日K, 但 `interval` 字段照写 "5m"
    → **日K 冒充分钟K**(前端画成"5分"图)。

本文件钉住三件事:
  ① 1mth/1M/月 → 真按月聚合(每月一根, 末根日期=该月最后一根交易日);
  ② 分钟周期(1m/5m/15m/30m/60m/intra) → 400 显式拒绝, 不返回任何 bar;
  ③ 指数与个股两条路径同样生效(指数分支过去会直接透传原始日K)。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.web.api import klines
from src.collectors.kline_collector import KlineData


def _bar(d: str, o=10.0, c=10.5, h=11.0, l=9.5, v=100.0):
    return KlineData(date=d, open=o, close=c, high=h, low=l, volume=v)


def _clone(k):
    """_aggregate_klines 返回新对象; 每个用例各拿一份, 免得共享实例被就地改写。"""
    return KlineData(date=k.date, open=k.open, close=k.close,
                     high=k.high, low=k.low, volume=k.volume)


DAILY = [
    _bar("2026-01-05", o=9.8, c=10.0, v=100.0),
    _bar("2026-01-27", o=11.5, c=12.0, v=200.0),
    _bar("2026-02-03", o=12.1, c=11.0, v=300.0),
    _bar("2026-02-27", o=12.8, c=13.0, v=400.0),
    _bar("2026-03-02", o=13.1, c=14.0, v=500.0),
]


# ── ① 月线真聚合 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("interval", ["1mth", "1M", "mth", "m", "month", "月"])
def test_month_interval_aggregates_to_monthly_bars(interval):
    out = klines._aggregate_klines([_clone(k) for k in DAILY], interval)
    assert len(out) == 3, f"{interval} 必须按月聚成 3 根, 实得 {len(out)} 根(日K透传=伪标)"
    # 月首开盘 / 月末收盘 / 区间高低 / 量求和
    # 月首开盘(该月第一根) / 月末收盘(该月最后一根) —— 不是"随便挑一根"
    assert (out[0].open, out[0].close) == (9.8, 12.0)
    assert (out[1].open, out[1].close) == (12.1, 13.0)
    assert out[2].open == 13.1
    assert out[0].volume == 300.0
    assert out[0].date == "2026-01-27"
    assert out[2].date == "2026-03-02"


def test_month_alias_matches_explicit_month_output():
    a = klines._aggregate_klines([_clone(k) for k in DAILY], "1mth")
    b = klines._aggregate_klines([_clone(k) for k in DAILY], "month")
    assert [(x.date, x.open, x.close, x.volume) for x in a] == [
        (x.date, x.open, x.close, x.volume) for x in b
    ]


# ── ② 分钟周期显式拒绝(不伪标) ─────────────────────────────────────────
@pytest.mark.parametrize("interval", ["5m", "15m", "30m", "60m", "intra", "1min", "分时"])
def test_minute_intervals_are_rejected_explicitly(interval):
    with pytest.raises(HTTPException) as ei:
        klines.resolve_interval(interval)
    assert ei.value.status_code == 400
    assert "分钟" in str(ei.value.detail)


def test_lowercase_1m_is_ambiguous_and_rejected_not_treated_as_month():
    """1m 既可能被读成 1 分钟, 旧实现却按月聚合 → 必须显式拒绝并指明月线写法。"""
    with pytest.raises(HTTPException) as ei:
        klines.resolve_interval("1m")
    detail = str(ei.value.detail)
    assert ei.value.status_code == 400
    assert "1mth" in detail


@pytest.mark.parametrize(
    "raw,expected",
    [("1d", "1d"), ("D", "1d"), ("1w", "1w"), ("WEEK", "1w"),
     ("1mth", "1mth"), ("1M", "1mth"), ("month", "1mth"), (None, "1d"), ("", "1d")],
)
def test_resolve_interval_normalizes_supported_periods(raw, expected):
    assert klines.resolve_interval(raw) == expected


def test_resolve_interval_rejects_garbage_instead_of_silently_daily():
    with pytest.raises(HTTPException) as ei:
        klines.resolve_interval("7d")
    assert ei.value.status_code == 400


# ── ③ 指数路径: 月线真聚合 / 分钟显式拒绝 ───────────────────────────────
def _fake_index_payload(monkeypatch, bars):
    """把腾讯指数K线响应灌进 market_get, 走真实指数分支(不打网络)。"""
    payload = {
        "data": {
            "sh000001": {
                "day": [["2026-01-05", "10", "10.5", "11", "9.5", "100"],
                        ["2026-01-27", "10.5", "12", "12.2", "10.4", "200"],
                        ["2026-02-27", "12", "13", "13.5", "11.8", "300"]],
            }
        }
    }
    calls: list[str] = []

    def fake_market_get(url, **kwargs):
        calls.append(url)
        if "fqkline" in url:
            return payload
        raise AssertionError(f"不应请求 {url}")

    monkeypatch.setattr("src.collectors.market_http.market_get", fake_market_get, raising=True)
    return calls


def test_index_month_interval_returns_monthly_not_daily(monkeypatch):
    _fake_index_payload(monkeypatch, None)
    body = klines._get_klines_uncached("000001", "CN", 120, "1mth", instrument="index")
    assert body["interval"] == "1mth"
    assert len(body["klines"]) == 2, "指数月K必须聚成 2 根, 实得 %d(日K透传)" % len(body["klines"])
    assert body["klines"][0]["date"] == "2026-01-27"
    assert body["klines"][0]["volume"] == 300.0


def test_index_minute_interval_rejected_before_any_fetch(monkeypatch):
    calls = _fake_index_payload(monkeypatch, None)
    with pytest.raises(HTTPException) as ei:
        klines._get_klines_uncached("000001", "CN", 120, "5m", instrument="index")
    assert ei.value.status_code == 400
    assert calls == [], "分钟周期必须在取数前拒绝, 不许把日K挂上分钟标签"


def test_stock_minute_interval_rejected_before_any_fetch(monkeypatch):
    from src.web.api import klines as kl

    fetched: list[str] = []
    monkeypatch.setattr(kl, "_pg_klines", lambda *a, **k: fetched.append("pg") or ([], None))
    with pytest.raises(HTTPException) as ei:
        kl._get_klines_uncached("600000", "CN", 120, "15m")
    assert ei.value.status_code == 400
    assert fetched == []


def test_response_interval_echoes_normalized_value(monkeypatch):
    """日K 请求 interval=1D 时, 回包必须写规范化后的 1d, 不让前端把别名当另一套周期。"""
    from src.web.api import klines as kl

    monkeypatch.setattr(kl, "_pg_klines", lambda *a, **k: (DAILY, "2026-03-02"))
    monkeypatch.setattr(kl, "_finalize_daily_bars", lambda bars, *a, **k: (bars, None))
    body = kl._get_klines_uncached("600000", "CN", 5, "1D")
    assert body["interval"] == "1d"
    assert len(body["klines"]) == 5


# ── ④ 真实 HTTP 路径(FastAPI 路由, 不 mock 路由层) ────────────────────────
def _http_client(monkeypatch, *, pg=None):
    """挂真实 router 的 TestClient; 只把「取数」换成本地样本, 路由/校验/序列化全走真代码。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(klines.router, prefix="/api/klines")
    _fake_index_payload(monkeypatch, None)
    if pg is not None:
        monkeypatch.setattr(klines, "_pg_klines", lambda *a, **k: ([_clone(x) for x in pg], "2026-03-02"))
        monkeypatch.setattr(klines, "_finalize_daily_bars", lambda bars, *a, **k: (bars, None))
    monkeypatch.setattr(klines, "_kl_cache", {})
    return TestClient(app)


def test_http_month_and_day_and_week_for_stock(monkeypatch):
    c = _http_client(monkeypatch, pg=DAILY)

    day = c.get("/api/klines/600000", params={"market": "CN", "days": 120, "interval": "1d"})
    assert day.status_code == 200, day.text
    assert day.json()["interval"] == "1d"
    assert len(day.json()["klines"]) == 5

    week = c.get("/api/klines/600000", params={"market": "CN", "days": 120, "interval": "1w"})
    assert week.status_code == 200, week.text
    # 周线按 ISO 自然周分桶 —— 期望值由样本自己算, 不写死(样本 5 天各在不同周)
    from datetime import datetime as _dt

    expect_weeks = {_dt.strptime(k.date, "%Y-%m-%d").isocalendar()[:2] for k in DAILY}
    assert len(week.json()["klines"]) == len(expect_weeks)

    month = c.get("/api/klines/600000", params={"market": "CN", "days": 120, "interval": "1mth"})
    assert month.status_code == 200, month.text
    assert month.json()["interval"] == "1mth"
    assert [b["date"] for b in month.json()["klines"]] == ["2026-01-27", "2026-02-27", "2026-03-02"]


def test_http_minute_returns_400_not_daily_bars(monkeypatch):
    c = _http_client(monkeypatch, pg=DAILY)
    r = c.get("/api/klines/600000", params={"market": "CN", "days": 120, "interval": "5m"})
    assert r.status_code == 400, r.text
    assert "分钟" in r.json()["detail"]
    assert "klines" not in r.json(), "400 必须不带任何 bar, 否则又是一次伪标"


def test_http_index_month_is_monthly_not_daily(monkeypatch):
    c = _http_client(monkeypatch)
    r = c.get("/api/klines/000001", params={"market": "CN", "days": 120,
                                            "interval": "1mth", "instrument": "index"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["is_index"] is True
    assert len(body["klines"]) == 2
    assert [b["date"] for b in body["klines"]] == ["2026-01-27", "2026-02-27"]


def test_http_index_day_still_daily(monkeypatch):
    c = _http_client(monkeypatch)
    r = c.get("/api/klines/000001", params={"market": "CN", "days": 120,
                                            "interval": "1d", "instrument": "index"})
    assert r.status_code == 200, r.text
    assert len(r.json()["klines"]) == 3


def test_http_index_minute_rejected(monkeypatch):
    c = _http_client(monkeypatch)
    r = c.get("/api/klines/000001", params={"market": "CN", "days": 120,
                                            "interval": "30m", "instrument": "index"})
    assert r.status_code == 400
    assert "分钟" in r.json()["detail"]
