"""撤单时序采样 + 逐笔分钟聚合（2026-10-21）。"""
import asyncio
from datetime import datetime

import pytest


# ── 逐笔 → 分钟 ─────────────────────────────────────────────

def test_tick_aggregate_basic():
    from src.core.tick_minute_flow import aggregate, summarize

    ticks = [
        {"d": "B", "amt": 1_000_000, "vol": 800, "t": "09:31:05"},
        {"d": "S", "amt": 300_000, "vol": 200, "t": "09:31:40"},
        {"d": "B", "amt": 2_000_000, "vol": 1_500, "t": "10:30:00"},
        {"d": "M", "amt": 500_000, "vol": 100, "t": "10:30:10"},
        {"d": "S", "amt": 4_000_000, "vol": 3_000, "t": "14:45:00"},
    ]
    rows = aggregate(ticks)
    assert [r["hhmm"] for r in rows] == ["0931", "1030", "1445"]
    assert rows[0]["net_amt"] == 700_000
    assert rows[0]["big_net_amt"] == 1_000_000, "≥500 手才算大单"
    s = summarize(rows)
    assert s["net_amt"] == -1_300_000
    assert s["tail_reached"] is True and s["tail_net_amt"] == -4_000_000


def test_tick_aggregate_skips_bad_time_and_neutral():
    from src.core.tick_minute_flow import aggregate

    rows = aggregate([
        {"d": "M", "amt": 100, "vol": 10, "t": "09:30:00"},
        {"d": "B", "amt": 999, "vol": 10, "t": "bad"},
    ])
    assert len(rows) == 1 and rows[0]["net_amt"] == 0, "M 不计净额，坏时间丢弃"


def test_tick_summary_lists_only_signed_minutes():
    from src.core.tick_minute_flow import aggregate, summarize

    ticks = [
        {"d": "B", "amt": 1_000_000, "vol": 100, "t": "09:31:00"},
        {"d": "S", "amt": 2_000_000, "vol": 100, "t": "09:32:00"},
    ]
    s = summarize(aggregate(ticks))
    assert [r["hhmm"] for r in s["inflow_top"]] == ["0931"]
    assert [r["hhmm"] for r in s["outflow_top"]] == ["0932"]


def test_tick_render_marks_missing_tail():
    from src.core.tick_minute_flow import aggregate, render, summarize

    ticks = [{"d": "B", "amt": 1_000_000, "vol": 100, "t": "09:31:00"}]
    text = render(summarize(aggregate(ticks)))
    assert "尚未到该时段" in text and "大单净额" in text


# ── 撤单采样 ─────────────────────────────────────────────

@pytest.fixture()
def db():
    from src.db.models import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng, tables=[
        t for name, t in Base.metadata.tables.items()
        if name in ("cancel_rate_snapshots",)
    ])
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()


def test_collect_once_skips_outside_window(db):
    from src.collectors.cancel_rate_sampler import collect_once

    assert collect_once(db, now=datetime(2026, 10, 21, 8, 0)) == 0
    assert collect_once(db, now=datetime(2026, 10, 24, 10, 0)) == 0  # 周六


def test_cancel_trend_needs_two_points(db):
    from src.collectors.cancel_rate_sampler import cancel_trend

    assert "采样不足" in cancel_trend(db, "002050")


def test_cancel_trend_reports_direction(db):
    from src.collectors.cancel_rate_sampler import cancel_trend
    from src.db.models import CancelRateSnapshot

    today = datetime.now().strftime("%Y%m%d")
    for hhmm, b, s in (("1030", 60.0, 40.0), ("1031", 85.0, 45.0)):
        db.add(CancelRateSnapshot(symbol="002050", trade_date=today, hhmm=hhmm,
                                  captured_at=datetime.now(), buy_cancel_rate=b,
                                  sell_cancel_rate=s, cancel_bias=0.2,
                                  intent="频繁试盘", source="tq_more_info"))
    db.commit()
    text = cancel_trend(db, "002050")
    assert "60.0% → 85.0%" in text and "↑+25.0pt" in text


def test_rotation_covers_all_symbols():
    from src.collectors.cancel_rate_sampler import _rotate

    syms = [f"{i:06d}" for i in range(50)]
    seen = set()
    for minute in range(0, 60, 1):
        seen.update(_rotate(syms, 20, datetime(2026, 10, 21, 10, minute)))
    assert seen == set(syms), "轮转必须能在几分钟内覆盖全部标的"
