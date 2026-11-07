"""测试 Copilot 定时任务四档时段语义、盘中循环、运行记录与存量行为（2026-11-03）。"""
from __future__ import annotations

from datetime import datetime
import pytest

from src.core import copilot_schedule as cs
from src.db.models import Base, CopilotSchedule, CopilotScheduleRun
from src.db.session import SessionLocal, engine


@pytest.fixture(autouse=True)
def setup_db(monkeypatch):
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(cs, "_deliver", lambda db, row, text: None)

    db = SessionLocal()
    try:
        db.query(CopilotScheduleRun).delete()
        db.query(CopilotSchedule).delete()
        db.commit()
    finally:
        db.close()
    yield


def test_premarket_window_trigger():
    db = SessionLocal()
    try:
        created = cs.create(
            db, "u1", kind="auction_digest", at_hhmm="09:25",
            window_kind="premarket"
        )
        sched_id = created["id"]

        # 1. 窗口外（08:50）不触发
        t_early = datetime(2026, 11, 3, 8, 50, 0)
        stats = cs.run_due(t_early)
        assert stats["ran"] == 0

        runs = cs.list_runs(db, "u1", sched_id)
        assert len(runs) == 0

        # 2. 窗口内（09:15）触发
        t_in = datetime(2026, 11, 3, 9, 15, 0)
        cs.run_due(t_in)
        runs = cs.list_runs(db, "u1", sched_id)
        assert len(runs) >= 1

        # 3. 同一天再次在窗口内（09:20）不重复触发
        cs.run_due(datetime(2026, 11, 3, 9, 20, 0))
        runs2 = cs.list_runs(db, "u1", sched_id)
        ran_count = sum(1 for r in runs2 if r["status"] == "ok")
        assert ran_count <= 1
    finally:
        db.close()


def test_postmarket_window_trigger():
    db = SessionLocal()
    try:
        created = cs.create(
            db, "u1", kind="daily_brief", at_hhmm="15:00",
            window_kind="postmarket"
        )
        sched_id = created["id"]

        # 1. 窗口外（14:50）不触发
        stats = cs.run_due(datetime(2026, 11, 3, 14, 50, 0))
        assert stats["ran"] == 0

        # 2. 窗口内（15:05）触发
        cs.run_due(datetime(2026, 11, 3, 15, 5, 0))
        runs = cs.list_runs(db, "u1", sched_id)
        assert len(runs) >= 1
    finally:
        db.close()


def test_intraday_loop_and_noon_break(monkeypatch):
    # `_build_text` 用 `date.today()` 判交易日：周末跑这条用例会整体 skipped（0 条 ok），
    # 与本用例要验的「盘中节奏」无关。钉死为交易日，避免按日历星期数变红。
    monkeypatch.setattr(cs, "is_trading_day", lambda _d: True)
    db = SessionLocal()
    try:
        created = cs.create(
            db, "u1", kind="auction_digest", at_hhmm="09:30",
            window_kind="intraday", interval_min=10
        )
        sched_id = created["id"]

        # 1. 上午盘中 09:35 触发
        t1 = datetime(2026, 11, 3, 9, 35, 0)
        cs.run_due(t1)
        runs1 = cs.list_runs(db, "u1", sched_id)
        ok_runs = [r for r in runs1 if r["status"] == "ok"]
        assert len(ok_runs) == 1

        # 2. 间隔内（09:40，仅过去 5 分钟，小于 interval_min=10）不触发
        t2 = datetime(2026, 11, 3, 9, 40, 0)
        cs.run_due(t2)
        runs2 = cs.list_runs(db, "u1", sched_id)
        assert len([r for r in runs2 if r["status"] == "ok"]) == 1

        # 3. 超过间隔（09:46，过去 11 分钟）再次触发
        t3 = datetime(2026, 11, 3, 9, 46, 0)
        cs.run_due(t3)
        runs3 = cs.list_runs(db, "u1", sched_id)
        assert len([r for r in runs3 if r["status"] == "ok"]) == 2

        # 4. 午休时间（12:00）不触发
        t_noon = datetime(2026, 11, 3, 12, 0, 0)
        cs.run_due(t_noon)
        runs_noon = cs.list_runs(db, "u1", sched_id)
        assert len([r for r in runs_noon if r["status"] == "ok"]) == 2
    finally:
        db.close()


def test_non_trading_day_skipped():
    db = SessionLocal()
    try:
        created = cs.create(
            db, "u1", kind="auction_digest", at_hhmm="09:25",
            window_kind="premarket"
        )
        sched_id = created["id"]

        # 2026-11-01 是周六（非交易日）
        t_sat = datetime(2026, 11, 1, 9, 25, 0)
        cs.run_due(t_sat)
        runs = cs.list_runs(db, "u1", sched_id)
        assert len(runs) == 0
    finally:
        db.close()


def test_national_day_holiday_does_not_trigger():
    db = SessionLocal()
    try:
        created = cs.create(db, "u1", kind="auction_digest", at_hhmm="09:30", window_kind="intraday")
        cs.run_due(datetime(2026, 10, 1, 9, 30))
        assert cs.list_runs(db, "u1", created["id"]) == []
    finally:
        db.close()


def test_custom_window_backward_compatibility():
    db = SessionLocal()
    try:
        created = cs.create(
            db, "u1", kind="auction_digest", at_hhmm="09:25",
            window_kind="custom"
        )
        sched_id = created["id"]

        # 09:24 不触发
        cs.run_due(datetime(2026, 11, 3, 9, 24, 0))
        assert len(cs.list_runs(db, "u1", sched_id)) == 0

        # 09:25 触发
        cs.run_due(datetime(2026, 11, 3, 9, 25, 0))
        runs = cs.list_runs(db, "u1", sched_id)
        assert len(runs) >= 1
    finally:
        db.close()
