"""作业框架测试(2026-09-12, 借鉴 TSP): 单飞复用 / 停滞自愈 / 协作取消 / 进度落库。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import src.core.jobs as J
import src.db.session as dbs


@pytest.fixture()
def store(monkeypatch):
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with eng.begin() as conn:
        from src.web.migrations import _m165_app_jobs_table

        _m165_app_jobs_table(conn)
    monkeypatch.setattr(dbs, "engine", eng)
    return J.JobStore()


def test_create_progress_and_read_back(store):
    jid, is_new = store.create("theme_mood_scan", "题材情绪分扫描")
    assert is_new is True
    store.start(jid, "scanning")
    store.progress(jid, 42, "scanning", "已扫 210/521 题材")
    row = store.get(jid)
    assert row["status"] == "running" and row["progress"] == 42
    assert row["message"] == "已扫 210/521 题材"
    store.succeed(jid, "rows=521")
    done = store.get(jid)
    assert done["status"] == "succeeded" and done["progress"] == 100 and done["finished_at"]
    assert store.active(kind="theme_mood_scan") == []          # 结束后不再活跃
    assert store.recent()[0]["id"] == jid                       # 近期列表按时间倒序


def test_single_flight_reuses_active_job(store):
    first, new1 = store.create("resonance_scan")
    second, new2 = store.create("resonance_scan")
    assert new1 is True and new2 is False
    assert first == second                                      # 重复点击不并发起第二个
    assert store.active() and len(store.active()) == 1
    store.succeed(first)
    third, new3 = store.create("resonance_scan")                # 结束后可再起
    assert new3 is True and third != first


def test_different_kinds_do_not_block_each_other(store):
    a, _ = store.create("theme_mood_scan")
    b, is_new = store.create("resonance_scan")
    assert is_new is True and a != b


def test_reap_stale_fails_frozen_jobs(store):
    from datetime import datetime, timedelta

    jid, _ = store.create("theme_mood_scan")
    store.start(jid)
    # 手工把 updated_at 推到停滞窗口之外
    old = datetime.now() - timedelta(seconds=J.STALL_SECONDS + 120)
    with dbs.engine.begin() as c:
        c.execute(text("UPDATE app_jobs SET updated_at = :t WHERE id = :i"), {"t": old, "i": jid})
    assert store.reap_stale() == 1
    row = store.get(jid)
    assert row["status"] == "failed" and "无进度更新" in row["error"]
    # 新鲜任务不被误杀(慢网络长任务不该按总时长判死)
    fresh, _ = store.create("market_phase_backfill")
    store.start(fresh)
    assert store.reap_stale() == 0
    assert store.get(fresh)["status"] == "running"


def test_cancel_only_affects_active_jobs(store):
    jid, _ = store.create("theme_mood_scan")
    store.start(jid)
    assert store.cancel(jid) is True
    assert store.get(jid)["status"] == "cancelled"
    assert store.is_cancelled(jid) is True
    assert store.cancel(jid) is False                           # 已结束不可再取消
    done, _ = store.create("resonance_scan")
    store.succeed(done)
    assert store.cancel(done) is False


def test_reads_survive_missing_table(monkeypatch):
    """新库/未迁移时不能抛 —— 列表返回空, 面板显示空态。"""
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(dbs, "engine", eng)
    s = J.JobStore()
    assert s.recent() == [] and s.active() == [] and s.get("nope") is None
