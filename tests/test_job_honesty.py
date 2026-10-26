"""作业状态诚实性测试(2026-10-08, 通达信断链三天无人发现的 P0-1 修复)。

扫描类任务用 `{"ok": False, "reason": ...}` 表达"没干成"(如 TQ 断链/数据源全空),
但过去作业 runner 只要函数不抛就 `jobs.succeed(...)` —— 面板显示成功、实际无数据。
本文件锁定: 返回体显式 `ok=False` 时作业必须 `failed` 且带原因。

覆盖: 统一 helper(`result_failure`/`JobStore.finish`)、theme_mood 三种 job、
resonance 手动扫描 job、market_data 进程内 `_bg_start` 后台任务。

禁令: 断言强度不得降低; 测试不触真实网络。
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import threading as _threading

import src.core.jobs as J
import src.db.session as dbs


class _ImmediateThread:
    """同步替身: .start() 直接在当前线程跑 target —— 消灭 job 测试的时序竞态。

    CI 实测(2026-10-08, run 37761685911): 真后台线程在负载下 5s 内不一定完成,
    `_wait_terminal` 超时红(单跑绿/CI 红)。runner 的正确性与线程调度无关,
    同步执行 = 断言强度不变、结果确定。
    """

    def __init__(self, target=None, name=None, daemon=None, **_kw):
        self._target = target

    def start(self) -> None:
        if self._target is not None:
            self._target()


@pytest.fixture(autouse=True)
def _sync_job_threads(monkeypatch):
    monkeypatch.setattr(_threading, "Thread", _ImmediateThread)
    yield


# ────────────────────────── 统一 helper 单测 ──────────────────────────

def test_result_failure_semantics():
    assert J.result_failure({"ok": False, "reason": "TQ 断链"}) == "TQ 断链"
    assert J.result_failure({"ok": False, "error": "boom"}) == "boom"
    assert J.result_failure({"ok": False}) == "任务返回 ok=False"
    assert J.result_failure({"ok": True}) is None
    assert J.result_failure({"scanned": 0}) is None      # 老任务体无 ok 键 → 不误伤
    assert J.result_failure(None) is None
    assert J.result_failure("str") is None


@pytest.fixture()
def store(monkeypatch):
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        from src.web.migrations import _m165_app_jobs_table

        _m165_app_jobs_table(conn)
    monkeypatch.setattr(dbs, "engine", eng)
    return J.JobStore()


def test_finish_fail_on_ok_false_message_and_error(store):
    jid, _ = store.create("unit", "单测")
    assert store.finish(jid, {"ok": False, "reason": "板块目录为空"}, context="扫描: ") is False
    row = store.get(jid)
    assert row["status"] == "failed"
    assert "板块目录为空" in row["message"] and "板块目录为空" in row["error"]
    assert row["finished_at"]


def test_finish_succeed_on_ok_true(store):
    jid, _ = store.create("unit", "单测")
    assert store.finish(jid, {"ok": True, "scanned": 3}) is True
    row = store.get(jid)
    assert row["status"] == "succeeded" and row["progress"] == 100


# ────────────────────────── P0-1 theme_mood 三种 job ──────────────────────────

def _wait_terminal(store, jid, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = store.get(jid)
        if row and row["status"] in J.TERMINAL:
            return row
        time.sleep(0.02)
    raise AssertionError(f"作业 {jid} 未在 {timeout}s 内终结")


@pytest.mark.parametrize(
    "spawn, patched_fn, kind",
    [
        ("_spawn_scan", "scan", "theme_mood_scan"),
        ("_spawn_intraday_refresh", "intraday_job", "theme_mood_intraday"),
        ("spawn_settle", "settle_job", "theme_mood_settle"),
    ],
)
def test_theme_mood_job_fails_when_scan_returns_ok_false(monkeypatch, store, spawn, patched_fn, kind):
    import src.core.theme_mood as tm
    import src.web.api.theme_mood as api

    monkeypatch.setattr(api, "jobs", store)
    # 三个 job 的真实入口都带参数, 统一用 **kw 兜
    monkeypatch.setattr(tm, patched_fn, lambda *a, **k: {"ok": False, "reason": "无日线数据"})

    out = getattr(api, spawn)()
    assert out["started"] is True
    row = _wait_terminal(store, out["job_id"])
    assert row["status"] == "failed"
    assert "无日线数据" in row["message"] and "无日线数据" in row["error"]
    assert store.active(kind=kind) == []


def test_theme_mood_job_succeeds_when_ok_true(monkeypatch, store, monkeypatch_tm_ok):
    import src.web.api.theme_mood as api

    monkeypatch.setattr(api, "jobs", store)
    out = api._spawn_scan()
    row = _wait_terminal(store, out["job_id"])
    assert row["status"] == "succeeded"


@pytest.fixture()
def monkeypatch_tm_ok(monkeypatch):
    import src.core.theme_mood as tm

    monkeypatch.setattr(tm, "scan", lambda *a, **k: {"ok": True, "rows": 12})
    return tm


# ────────────────────────── P0-1 market_data._bg_start ──────────────────────────

def test_bg_start_fails_when_fn_returns_ok_false(monkeypatch):
    import src.web.api.market_data as md

    md._bg_jobs.clear()
    md._bg_start("honesty_fail", lambda: {"ok": False, "reason": "数据源全空"})
    deadline = time.time() + 5.0
    st = md._bg_job_status("honesty_fail")
    while time.time() < deadline and st["status"] == "running":
        time.sleep(0.02)
        st = md._bg_job_status("honesty_fail")
    assert st["status"] == "failed"
    assert "数据源全空" in st["error"]


def test_bg_start_succeeds_when_ok_true(monkeypatch):
    import src.web.api.market_data as md

    md._bg_jobs.clear()
    md._bg_start("honesty_ok", lambda: {"ok": True, "count": 1})
    deadline = time.time() + 5.0
    st = md._bg_job_status("honesty_ok")
    while time.time() < deadline and st["status"] == "running":
        time.sleep(0.02)
        st = md._bg_job_status("honesty_ok")
    assert st["status"] == "succeeded"


def test_bg_start_still_fails_on_exception(monkeypatch):
    import src.web.api.market_data as md

    def _boom():
        raise RuntimeError("kaboom")

    md._bg_jobs.clear()
    md._bg_start("honesty_exc", _boom)
    deadline = time.time() + 5.0
    st = md._bg_job_status("honesty_exc")
    while time.time() < deadline and st["status"] == "running":
        time.sleep(0.02)
        st = md._bg_job_status("honesty_exc")
    assert st["status"] == "failed" and "kaboom" in st["error"]


# ────────────────────────── P0-1 resonance job ──────────────────────────

def test_resonance_job_fails_when_scan_ok_false(monkeypatch, store):
    import src.core.resonance_scan as rscan
    import src.web.api.resonance as rapi

    monkeypatch.setattr(rapi, "jobs", store)
    monkeypatch.setattr(rscan, "scan", lambda **k: {"ok": False, "reason": "TQ 日线全断",
                                                    "chunks_failed": 3, "chunks_total": 3})
    out = rapi.run_scan(limit=10)
    row = _wait_terminal(store, out["job_id"])
    assert row["status"] == "failed" and "TQ 日线全断" in row["message"]


def test_resonance_job_succeeded_but_reports_incomplete(monkeypatch, store):
    """部分分片失败(ok=True + complete=False)不静默: 作业成功但终态 message 显式带
    complete=False 与 note(消费方据此不得当全市场口径用)。"""
    import src.core.resonance_scan as rscan
    import src.web.api.resonance as rapi

    monkeypatch.setattr(rapi, "jobs", store)
    monkeypatch.setattr(rscan, "scan", lambda **k: {
        "ok": True, "scanned": 5000, "complete": False,
        "note": "分片失败: 命中数不是全市场口径(日线失败 2/60 片)",
        "chunks_failed": 2, "chunks_total": 60,
    })
    out = rapi.run_scan(limit=10)
    row = _wait_terminal(store, out["job_id"])
    assert row["status"] == "succeeded"
    assert "complete" in row["message"] and "False" in row["message"]
    assert "不是全市场口径" in row["message"]

