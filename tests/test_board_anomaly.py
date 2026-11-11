"""板块异动记录(2026-11-11): 判定/去重/写入/覆盖度。

口径与热力图芯片一致: 急拉急跌 = 5 分钟涨速 |≥0.5%|, 放量 = 量比 ≥2.0。
重点验证「不编造」: 没有历史快照时算不出涨速就**不许**记急拉; 非交易时段不写库。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from src.core import board_anomaly as ba

CST = ZoneInfo("Asia/Shanghai")


def _now() -> datetime:
    return datetime(2026, 11, 11, 10, 30, tzinfo=CST)


def _hist(code: str, price: float, ago_s: float = 300.0, now: float | None = None) -> dict:
    now_ts = (now if now is not None else _now().timestamp())
    return {code: [(now_ts - ago_s, price)]}


# ── 判定 ───────────────────────────────────────────────────────────────────────


def test_surge_when_five_minute_speed_above_threshold():
    cur = {"881290.SH": {"name": "半导体", "board_type": "concept", "price": 100.6, "change_pct": 2.0}}
    ev = ba.detect_events(cur, _hist("881290.SH", 100.0), now_ts=_now().timestamp(), progress=0.4)
    assert [e["kind"] for e in ev] == ["surge"]
    assert ev[0]["value"] == pytest.approx(0.6, abs=0.01)
    assert ev[0]["window_min"] == 5


def test_dump_when_speed_below_negative_threshold():
    cur = {"881171.SH": {"name": "包装印刷", "board_type": "industry", "price": 99.0, "change_pct": -1.0}}
    ev = ba.detect_events(cur, _hist("881171.SH", 100.0), now_ts=_now().timestamp(), progress=0.4)
    assert [e["kind"] for e in ev] == ["dump"]
    assert ev[0]["value"] == pytest.approx(-1.0, abs=0.01)


def test_no_event_when_speed_inside_band():
    cur = {"881290.SH": {"name": "半导体", "board_type": "concept", "price": 100.4, "change_pct": 0.4}}
    ev = ba.detect_events(cur, _hist("881290.SH", 100.0), now_ts=_now().timestamp(), progress=0.4)
    assert ev == []


def test_no_speed_event_without_history():
    """没有 5 分钟前的快照 → 算不出涨速 → 绝不凭当前涨幅猜一个「急拉」。"""
    cur = {"881290.SH": {"name": "半导体", "board_type": "concept", "price": 106.0, "change_pct": 6.0}}
    assert ba.detect_events(cur, {}, now_ts=_now().timestamp(), progress=0.4) == []


def test_stale_history_outside_tolerance_is_not_a_speed():
    """20 分钟前的点位不能当 5 分钟基准(否则把全天涨幅说成「5 分钟急拉」)。"""
    cur = {"881290.SH": {"name": "半导体", "board_type": "concept", "price": 106.0, "change_pct": 6.0}}
    ev = ba.detect_events(cur, _hist("881290.SH", 100.0, ago_s=1200), now_ts=_now().timestamp(), progress=0.4)
    assert ev == []


def test_heavy_volume_uses_ratio_against_baseline_and_progress():
    cur = {"881290.SH": {"name": "半导体", "board_type": "concept", "price": 100.0,
                         "change_pct": 0.0, "amount": 3.0e8}}
    ev = ba.detect_events(
        cur, {}, now_ts=_now().timestamp(), progress=0.5, baseline={"881290.SH": 2.0e8}
    )
    # 3e8 / (2e8 * 0.5) = 3.0 ≥ 2.0
    assert [e["kind"] for e in ev] == ["heavy_volume"]
    assert ev[0]["value"] == pytest.approx(3.0, abs=0.01)


def test_heavy_volume_requires_baseline():
    cur = {"881290.SH": {"name": "半导体", "board_type": "concept", "price": 100.0,
                         "change_pct": 0.0, "amount": 3.0e8}}
    assert ba.detect_events(cur, {}, now_ts=_now().timestamp(), progress=0.5, baseline={}) == []


def test_missing_price_skips_speed_but_still_checks_volume():
    cur = {"881290.SH": {"name": "半导体", "board_type": "concept", "price": None,
                         "change_pct": None, "amount": 3.0e8}}
    ev = ba.detect_events(
        cur, _hist("881290.SH", 100.0), now_ts=_now().timestamp(), progress=0.5,
        baseline={"881290.SH": 1.0e8},
    )
    assert [e["kind"] for e in ev] == ["heavy_volume"]


# ── 落库 ───────────────────────────────────────────────────────────────────────


def _tables_ready():
    from src.db.session import engine

    with engine.begin() as conn:
        for t in ("board_anomaly_snapshots", "board_anomaly_records"):
            conn.execute(text(f"DELETE FROM {t}"))


def test_sample_once_skips_outside_trading_window(monkeypatch):
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("非交易时段不应该取数")

    monkeypatch.setattr(ba, "sample_once", ba.sample_once)  # 保持引用, 下面直接调用
    out = ba.sample_once(
        now=datetime(2026, 11, 11, 20, 0, tzinfo=CST),
        quotes_fn=_boom,
        sectors_fn=lambda: [],
    )
    assert out["ok"] is False and out["skipped"] == "not_trading_window"
    assert called["n"] == 0


def test_sample_once_writes_snapshot_and_detects_surge_on_second_round():
    """两轮真实采样(注入) → 第一轮只落快照, 第二轮因有 5 分钟基准而命中急拉。"""
    _tables_ready()
    t0 = _now()
    price = {"v": 100.0}

    def _quotes(codes):
        return {c: {"price": price["v"], "change_pct": (price["v"] / 100.0 - 1) * 100,
                    "amount": 1.0e8, "fund_net": 0.0} for c in codes}

    sectors = [
        {"code": "881290.SH", "name": "半导体", "board_type": "concept"},
        {"code": "881171.SH", "name": "包装印刷", "board_type": "industry"},
    ]

    r1 = ba.sample_once(now=t0, quotes_fn=_quotes, sectors_fn=lambda: sectors,
                        baseline_fn=lambda codes: {}, progress_fn=lambda: 0.5, force=True)
    assert r1["ok"] and r1["boards"] == 2 and r1["events"] == 0

    price["v"] = 100.8  # +0.8% / 5min
    r2 = ba.sample_once(now=t0 + timedelta(minutes=5), quotes_fn=_quotes, sectors_fn=lambda: sectors,
                        baseline_fn=lambda codes: {}, progress_fn=lambda: 0.5, force=True)
    assert r2["ok"]
    assert r2["events"] == 2 and r2["inserted"] == 2

    from src.db.session import engine

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT block_code, kind, value FROM board_anomaly_records ORDER BY block_code"
        )).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("881171.SH", "surge"), ("881290.SH", "surge")]
    assert all(r[2] == pytest.approx(0.8, abs=0.02) for r in rows)


def test_sample_once_dedupes_same_kind_within_15_minutes():
    _tables_ready()
    t0 = _now()
    price = {"v": 100.0}

    def _quotes(codes):
        return {c: {"price": price["v"], "change_pct": 0.0, "amount": 1.0e8, "fund_net": 0.0} for c in codes}

    sectors = [{"code": "881290.SH", "name": "半导体", "board_type": "concept"}]
    ba.sample_once(now=t0, quotes_fn=_quotes, sectors_fn=lambda: sectors,
                   baseline_fn=lambda c: {}, progress_fn=lambda: 0.5, force=True)
    price["v"] = 100.9
    # 同一 15 分钟桶内再命中一次 → 不写第二行
    ba.sample_once(now=t0 + timedelta(minutes=3), quotes_fn=_quotes, sectors_fn=lambda: sectors,
                   baseline_fn=lambda c: {}, progress_fn=lambda: 0.5, force=True)
    from src.db.session import engine

    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM board_anomaly_records")).scalar()
    assert n == 1


def test_read_events_and_coverage_shape():
    _tables_ready()
    t0 = _now()

    def _quotes(codes):
        return {c: {"price": 100.0, "change_pct": 1.0, "amount": 1.0e8, "fund_net": 0.0} for c in codes}

    sectors = [{"code": "881290.SH", "name": "半导体", "board_type": "concept"}]
    ba.sample_once(now=t0, quotes_fn=_quotes, sectors_fn=lambda: sectors,
                   baseline_fn=lambda c: {}, progress_fn=lambda: 0.5, force=True)

    from src.db.session import engine

    with engine.connect() as conn:
        cov = ba.read_coverage(conn, trade_date="20261111")
    assert cov["samples"] == 1 and cov["boards"] == 1
    assert cov["first_ts"] and cov["first_ts"].startswith("2026-11-11T10:30")
    assert cov["interval_s"] == ba.SCAN_INTERVAL_S

    with engine.connect() as conn:
        items = ba.read_events(conn, trade_date="20261111", board_type="concept", limit=10)
    assert items == []  # 单轮采样没有历史基准 → 没有急拉记录(不编造)


# ── 接口 ───────────────────────────────────────────────────────────────────────


def _api_client():
    """独立内存库 + 只跑 m246 迁移(接口只读这两张表)。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import src.web.api.boards as boards_api
    from src.web import models as M  # noqa: F401
    from src.web.database import get_db
    from src.web.migrations import _m246_board_anomaly

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    with engine.begin() as conn:
        _m246_board_anomaly(conn)
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(boards_api.router, prefix="/api/boards")

    def _db():
        sess = Session()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app), Session


def test_api_no_sampling_is_unavailable_not_empty_success():
    client, _ = _api_client()
    body = client.get("/api/boards/anomalies?date=20261111").json()
    assert body["available"] is False
    assert body["coverage"]["samples"] == 0
    assert body["items"] == []
    assert "暂无采样" in body["note"]


def test_api_returns_records_with_label_and_type_filter():
    client, Session = _api_client()
    db = Session()
    db.execute(text(
        "INSERT INTO board_anomaly_snapshots (trade_date, ts, block_code, board_type, price, change_pct, amount, fund_net) "
        "VALUES ('20261111', '2026-11-11 10:30:00', '881290.SH', 'concept', 100.0, 2.0, 1e8, 0)"
    ))
    db.execute(text(
        "INSERT INTO board_anomaly_snapshots (trade_date, ts, block_code, board_type, price, change_pct, amount, fund_net) "
        "VALUES ('20261111', '2026-11-11 10:30:00', '881171.SH', 'industry', 100.0, 1.0, 1e8, 0)"
    ))
    db.execute(text(
        "INSERT INTO board_anomaly_records (trade_date, ts, block_code, name, board_type, kind, value, window_min, "
        "change_pct, fund_net, bucket, source) VALUES "
        "('20261111', '2026-11-11 10:30:00', '881290.SH', '半导体', 'concept', 'surge', 0.62, 5, 2.0, NULL, 1, 'tdx_tq')"
    ))
    db.execute(text(
        "INSERT INTO board_anomaly_records (trade_date, ts, block_code, name, board_type, kind, value, window_min, "
        "change_pct, fund_net, bucket, source) VALUES "
        "('20261111', '2026-11-11 10:31:00', '881171.SH', '包装印刷', 'industry', 'heavy_volume', 2.4, NULL, 1.0, NULL, 1, 'tdx_tq')"
    ))
    db.commit()
    db.close()

    body = client.get("/api/boards/anomalies?date=20261111").json()
    assert body["available"] is True and body["count"] == 2
    kinds = {i["block_code"]: (i["kind"], i["label"]) for i in body["items"]}
    assert kinds["881290.SH"] == ("surge", "急拉")
    assert kinds["881171.SH"] == ("heavy_volume", "放量")
    assert body["coverage"]["samples"] == 1 and body["coverage"]["boards"] == 2

    only_concept = client.get("/api/boards/anomalies?date=20261111&type=concept").json()
    assert [i["block_code"] for i in only_concept["items"]] == ["881290.SH"]


def test_api_rejects_bad_type_and_date():
    client, _ = _api_client()
    assert client.get("/api/boards/anomalies?type=xxx").status_code == 400
    assert client.get("/api/boards/anomalies?date=2026-1-1").status_code == 400

def test_heavy_volume_suppressed_in_first_ten_minutes():
    """开盘头十分钟不判放量: 进度 0.05 的保底会把量比抬到 2 倍以上, 全员"放量"没信息量。"""
    cur = {"881290.SH": {"name": "半导体", "board_type": "concept", "price": 100.0,
                         "change_pct": 0.0, "amount": 3.0e8}}
    ev = ba.detect_events(
        cur, {}, now_ts=_now().timestamp(), progress=0.05, baseline={"881290.SH": 2.0e8}
    )
    assert ev == []
