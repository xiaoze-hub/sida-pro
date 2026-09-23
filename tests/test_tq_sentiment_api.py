"""市场情绪日序列 API 测试(2026-09-23)。

不联网: 表数据直接写, 拉取路径 monkeypatch 掉。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.web.api import sentiment_series as api

PREFIX = "/api/market/sentiment"


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(api.router, prefix=PREFIX)
    return TestClient(app)


def _trading_days(n: int, end: str = "20260923") -> list[str]:
    """从 end 往前取 n 个交易日(只跳周末, 够用)。"""
    d = date(int(end[:4]), int(end[4:6]), int(end[6:]))
    out: list[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return sorted(out)


@pytest.fixture()
def seeded():
    """造 30 个交易日: 涨停 80 家, 其中最后一天掉到 30 家(退潮)。"""
    from src.web.database import SessionLocal

    days = _trading_days(30)
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM market_sentiment_daily"))
        for i, d in enumerate(days):
            limit_up = 30 if i == len(days) - 1 else 80
            db.execute(
                text(
                    "INSERT INTO market_sentiment_daily "
                    "(trade_date, market, limit_up_count, limit_up_open_count, "
                    " streak_count, seal_success_money, seal_fail_money, source) "
                    "VALUES (:d, 'CN', :lu, 20, 12, 500.0, 300.0, 'tdx_tq')"
                ),
                {"d": d, "lu": limit_up},
            )
        db.commit()
    finally:
        db.close()
    yield days
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM market_sentiment_daily"))
        db.commit()
    finally:
        db.close()


def test_health_reports_rows_and_range(client, seeded):
    r = client.get(f"{PREFIX}/health")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["available"] is True
    assert d["rows"] == 30
    assert d["first"] == seeded[0] and d["last"] == seeded[-1]
    assert d["source"] == "tdx_tq"


def test_health_when_empty(client):
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM market_sentiment_daily"))
        db.commit()
    finally:
        db.close()
    d = client.get(f"{PREFIX}/health").json()
    assert d["available"] is False and d["rows"] == 0


def test_baseline_returns_current_and_history(client, seeded):
    r = client.get(f"{PREFIX}/baseline", params={"days": 20})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["asof"] == seeded[-1] == d["latest"]["trade_date"]
    assert d["sample_days"] == 20
    b = d["baseline"]["limit_up_count"]
    assert b["current"] == 30 and b["current_date"] == seeded[-1]
    assert b["mean"] == pytest.approx(80.0)
    assert b["min"] == 80 and b["max"] == 80
    assert b["pct_rank"] == 0.0, "30 家低于历史全部 80 家 → 分位 0"


def test_baseline_404_when_no_data(client):
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM market_sentiment_daily"))
        db.commit()
    finally:
        db.close()
    r = client.get(f"{PREFIX}/baseline")
    assert r.status_code == 404
    assert "无情绪序列" in r.json()["detail"]


@pytest.mark.parametrize("bad", [1, 4, 251, 999])
def test_baseline_rejects_out_of_range_days(client, bad):
    assert client.get(f"{PREFIX}/baseline", params={"days": bad}).status_code == 422


def test_series_is_ascending_with_iso_date(client, seeded):
    r = client.get(f"{PREFIX}/series", params={"days": 5})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] == 5
    dates = [x["trade_date"] for x in d["items"]]
    assert dates == sorted(dates), "必须升序"
    assert dates[-1] == seeded[-1]
    assert d["items"][-1]["trade_date_iso"] == (
        f"{seeded[-1][:4]}-{seeded[-1][4:6]}-{seeded[-1][6:]}"
    )
    # 只暴露白名单列, 不带 id/market/extra 等内部字段
    assert set(d["items"][0]) == set(api._SERIES_COLUMNS) | {"trade_date_iso"}


def test_series_empty_returns_zero(client):
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM market_sentiment_daily"))
        db.commit()
    finally:
        db.close()
    d = client.get(f"{PREFIX}/series").json()
    assert d["count"] == 0 and d["items"] == []


def test_sync_delegates_with_default_window(client, monkeypatch):
    import src.collectors.tq_sentiment_series as tss

    seen: dict = {}

    def fake(db, *, days=0):
        seen["days"] = days
        return {"rows": 3, "first": "20260901", "last": "20260903", "tables": 31}

    monkeypatch.setattr(tss, "sync_sentiment_series", fake)
    r = client.post(f"{PREFIX}/sync")
    assert r.status_code == 200, r.text
    assert r.json()["rows"] == 3
    assert seen["days"] == 30, "默认回看 30 天"


def test_sync_backfill_uses_420(client, monkeypatch):
    import src.collectors.tq_sentiment_series as tss

    seen: dict = {}
    monkeypatch.setattr(
        tss,
        "sync_sentiment_series",
        lambda db, *, days=0: (seen.update(days=days), {"rows": 282})[1],
    )
    assert client.post(f"{PREFIX}/sync", params={"backfill": 1}).status_code == 200
    assert seen["days"] == 420


def test_sync_502_on_collector_error(client, monkeypatch):
    import src.collectors.tq_sentiment_series as tss

    monkeypatch.setattr(
        tss,
        "sync_sentiment_series",
        lambda db, *, days=0: {"error": "TQ 网关不可达"},
    )
    r = client.post(f"{PREFIX}/sync")
    assert r.status_code == 502
    assert "TQ 网关不可达" in r.json()["detail"]
