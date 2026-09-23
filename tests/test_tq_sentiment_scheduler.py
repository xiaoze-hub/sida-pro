"""TQ 情绪序列调度器测试(2026-09-23)。不联网。"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import text

from src.core import tq_sentiment_scheduler as sched_mod
from src.core.tq_sentiment_scheduler import (
    BACKFILL_DAYS,
    MIN_SAMPLE_ROWS,
    TqSentimentScheduler,
    _is_market_day,
    _needs_backfill,
)


def test_is_market_day_returns_bool():
    assert isinstance(_is_market_day(), bool)


@pytest.mark.parametrize("count,expect", [(0, True), (MIN_SAMPLE_ROWS - 1, True), (MIN_SAMPLE_ROWS, False), (500, False)])
def test_needs_backfill_threshold(count, expect):
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM market_sentiment_daily"))
        for i in range(count):
            db.execute(
                text(
                    "INSERT INTO market_sentiment_daily (trade_date, market, source) "
                    "VALUES (:d, 'CN', 'tdx_tq')"
                ),
                {"d": f"2026{(i // 28 + 1):02d}{(i % 28 + 1):02d}"},
            )
        db.commit()
        assert _needs_backfill(db) is expect
    finally:
        db.execute(text("DELETE FROM market_sentiment_daily"))
        db.commit()
        db.close()


def test_needs_backfill_returns_false_when_table_missing():
    """迁移未跑/表不可读时不该炸掉启动流程, 返回 False 跳过回补。"""
    class _Broken:
        def execute(self, *a, **k):
            raise RuntimeError("no such table")

    assert _needs_backfill(_Broken()) is False


async def _noop() -> None:
    return None


async def test_start_registers_cron_and_registry(monkeypatch):
    s = TqSentimentScheduler(timezone="Asia/Shanghai")
    # 启动回补会真的打网关 → 换成无副作用协程(用 async def, 避免 un-awaited coroutine 警告)
    monkeypatch.setattr(s, "_startup_backfill", _noop)
    try:
        s.start()
        jobs = {j.id for j in s.scheduler.get_jobs()}
        assert "tq_sentiment_daily" in jobs, "收盘后 cron 必须注册"
        assert "tq_sentiment_boot_backfill" in jobs, "启动回补 job 必须注册"

        from src.core.scheduler_registry import get_all

        assert "tq_sentiment" in get_all(), "要进注册表(系统自检靠它判调度器是否在跑)"
    finally:
        s.shutdown()
        await asyncio.sleep(0)


async def test_daily_job_skips_when_previous_still_running(monkeypatch):
    s = TqSentimentScheduler()
    called: list[int] = []
    monkeypatch.setattr(
        sched_mod, "_sync_in_job", lambda **k: called.append(1) or {"rows": 0}
    )
    s._running = True  # 模拟上轮未结束
    await s._job()
    assert called == [], "上一轮还在跑时必须跳过本轮"


async def test_daily_job_skips_non_market_day(monkeypatch):
    s = TqSentimentScheduler()
    called: list[int] = []
    monkeypatch.setattr(sched_mod, "_is_market_day", lambda: False)
    monkeypatch.setattr(
        sched_mod, "_sync_in_job", lambda **k: called.append(1) or {"rows": 0}
    )
    await s._job()
    assert called == [], "周末不该拉数据"


async def test_daily_job_runs_and_resets_flag(monkeypatch):
    s = TqSentimentScheduler()
    monkeypatch.setattr(sched_mod, "_is_market_day", lambda: True)
    monkeypatch.setattr(sched_mod, "_sync_in_job", lambda **k: {"rows": 282})
    await s._job()
    assert s._running is False, "跑完必须复位, 否则后续全被跳过"


async def test_daily_job_swallows_error(monkeypatch):
    """拉取失败只记日志, 绝不抛(抛了会掀翻 APScheduler 的 job)。"""
    s = TqSentimentScheduler()

    def boom(**k):
        raise RuntimeError("TQ 网关不可达")

    monkeypatch.setattr(sched_mod, "_is_market_day", lambda: True)
    monkeypatch.setattr(sched_mod, "_sync_in_job", boom)
    await s._job()  # 不应抛
    assert s._running is False


def test_trigger_now_defaults_to_short_window(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        sched_mod,
        "_sync_in_job",
        lambda **k: (seen.update(k), {"rows": 3})[1],
    )
    out = TqSentimentScheduler().trigger_now()
    assert out == {"rows": 3}
    assert seen == {"backfill": False}


def test_trigger_now_backfill(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(
        sched_mod,
        "_sync_in_job",
        lambda **k: (seen.update(k), {"rows": 282})[1],
    )
    out = TqSentimentScheduler().trigger_now(backfill=True)
    assert out == {"rows": 282}
    assert seen == {"backfill": True}


def test_constants_are_sane():
    assert 15 <= 15 and sched_mod.RUN_HOUR == 15 and sched_mod.RUN_MINUTE == 35
    assert BACKFILL_DAYS > MIN_SAMPLE_ROWS > 0
    # 收盘 15:00 之后才取, 否则当日 SC 值未定稿
    assert sched_mod.RUN_HOUR >= 15
    assert isinstance(datetime.now(), datetime)
