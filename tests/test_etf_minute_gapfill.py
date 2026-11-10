"""ETF 分钟「TQ 空源」自动补缺（调度侧）的隔离测试。

钉死的东西（每一条写错都是静默脏数据，不会抛异常）：

1. 只补**已闭市**且该日**0 行**的交易日；已有任何行（含用户数据）一次 backfill 都不调；
2. 自动路径**必须** `require_verified=True` + 非空 `daily_ref`，源码里不允许出现
   跳过对账的入口；
3. 假日 / 残缺日 / 取数失败都是显式失败（`ok=False` + 原因 + 可观测事件）；
4. 有限窗口重试：只对瞬时类失败重试，次数/退避/整轮 deadline 都有上界；
5. 启动恢复不阻塞（daemon 线程 + 立即返回），交易时段跳过；
6. 调度注册只挂 cron，装配期零 I/O。

数据库一律 tmp_path 下的独立 SQLite（真实迁移 `_m243` / `_m245`），并在夹具里
**强制剔除**所有数据库环境变量，避免误连生产/本地库。
"""

from __future__ import annotations

import inspect
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from src.core import etf_minute_gapfill as gap
from src.collectors import em_etf_minute as em

_REPO = Path(__file__).resolve().parents[1]
_BASKET = [c for c, _n, _cat in __import__("src.collectors.tq_etf_watch", fromlist=["x"]).ETF_BASKET]
_CODES = _BASKET[:3]
_DAY = "20261106"          # 2026-11-06 周五（交易日，已闭市）
_TODAY = datetime(2026, 11, 10, 20, 0)  # 2026-11-10 周二


# ── 夹具：环境隔离 + 独立库 + 观测事件收集 ──────────────────────────────

@pytest.fixture()
def clean_db_env(monkeypatch):
    """强制剔除数据库环境变量：测试只允许用 tmp_path 的独立 SQLite。"""
    for key in (
        "SIDA_DB_URL",
        "SIDA_DB",
        "DATABASE_URL",
        "DATABASE_URL_WRITE",
        "DATABASE_URL_READ",
        "DATABASE_URL_MIGRATE",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture()
def iso_engine(clean_db_env, tmp_path):
    from src.web.migrations import _m243_etf_minute_flow, _m245_etf_minute_source

    eng = create_engine(f"sqlite:///{tmp_path / 'etf_gapfill.db'}")
    with eng.begin() as conn:
        _m243_etf_minute_flow(conn)
        _m245_etf_minute_source(conn)
    yield eng
    eng.dispose()


@pytest.fixture()
def gap_events(monkeypatch):
    """把可观测事件收集起来，避免测试往 DATA_DIR 写 JSONL。"""
    events: list[tuple[str, str, str, dict]] = []

    def _fake(day, kind, detail, **ctx):
        events.append((str(day), str(kind), str(detail), dict(ctx)))

    monkeypatch.setattr(gap, "_publish_gap_event", _fake)
    monkeypatch.setattr(gap, "reset_state", gap.reset_state)
    gap.reset_state()
    yield events
    gap.reset_state()


def _insert(eng, day: str, code: str, minutes, source: str | None = "tdx_tq") -> None:
    with eng.begin() as conn:
        for hm in minutes:
            conn.execute(
                text(
                    "INSERT INTO etf_minute_flow "
                    "(trade_date, minute, etf_code, amount_wan, volume, source) "
                    "VALUES (:d, :m, :c, 1.0, 100, :s)"
                ),
                {"d": day, "m": hm, "c": code, "s": source},
            )


def _count(eng, day: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM etf_minute_flow"
    params: dict = {}
    if day:
        sql += " WHERE trade_date = :d"
        params["d"] = day
    with eng.connect() as conn:
        return int(conn.execute(text(sql), params).scalar() or 0)


def _no_backfill(monkeypatch):
    """任何 backfill 调用都直接炸：用来证明"这一天根本不该去补"。"""

    def _boom(*a, **kw):
        raise AssertionError(f"不该调用 backfill: {a} {kw}")

    monkeypatch.setattr(em, "backfill", _boom)


# ── 1. 分类：假日/周末/未闭市/完整/残缺/空 ──────────────────────────────

def test_holiday_is_explicit_failure(iso_engine, monkeypatch, gap_events):
    _no_backfill(monkeypatch)
    rep = gap.run_gapfill(["20261005"], engine=iso_engine, now=_TODAY, codes=_CODES)
    day = rep["days"][0]
    assert day["state"] == gap.ST_HOLIDAY
    assert rep["ok"] is False
    assert rep["failed_days"] == ["20261005"]
    assert gap_events and gap_events[0][0] == "20261005"
    assert gap_events[0][1] == "non_trading_day"


def test_weekend_is_explicit_failure(iso_engine, monkeypatch):
    _no_backfill(monkeypatch)
    rep = gap.run_gapfill(["20261107"], engine=iso_engine, now=_TODAY, codes=_CODES)
    assert rep["days"][0]["state"] == gap.ST_HOLIDAY
    assert rep["ok"] is False


def test_today_is_deferred_not_failure(iso_engine, monkeypatch):
    _no_backfill(monkeypatch)
    rep = gap.run_gapfill(["20261110"], engine=iso_engine, now=_TODAY, codes=_CODES)
    day = rep["days"][0]
    assert day["state"] == gap.ST_DEFERRED_TODAY
    assert day["result"] == "deferred_today"
    assert rep["ok"] is True and rep["deferred_days"] == ["20261110"]


def test_complete_day_is_not_touched(iso_engine, monkeypatch):
    _insert(iso_engine, _DAY, _CODES[0], em.SESSION_MINUTES, source="tdx_tq")
    _no_backfill(monkeypatch)
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=[_CODES[0]])
    day = rep["days"][0]
    assert day["state"] == gap.ST_COMPLETE
    assert rep["ok"] is True
    assert rep["written_days"] == []
    assert _count(iso_engine) == len(em.SESSION_MINUTES)


def test_partial_day_fails_without_calling_backfill(iso_engine, monkeypatch, gap_events):
    """有 1 行 ≠ 可补。残缺日必须报失败，且一行都不许动（不覆盖用户数据）。"""
    _insert(iso_engine, _DAY, _CODES[0], ["0931"], source="tdx_tq")
    _no_backfill(monkeypatch)
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES)
    day = rep["days"][0]
    assert day["state"] == gap.ST_PARTIAL
    assert day["result"] == "partial"
    assert rep["ok"] is False and rep["failed_days"] == [_DAY]
    assert _count(iso_engine) == 1
    assert [e[1] for e in gap_events] == ["partial_day"]


def test_plan_excludes_today_and_lists_closed_days(iso_engine):
    plan = gap.plan_gapfill(days=4, engine=iso_engine, now=_TODAY, codes=_CODES)
    dates = [d["date"] for d in plan["days"]]
    assert dates == ["20261105", "20261106", "20261109"]
    assert "20261110" not in dates
    assert plan["pending"] == dates


# ── 2. 补缺：只走 require_verified + daily_ref，绝不放开对账 ─────────────

def test_empty_day_backfills_with_verified_reconcile(iso_engine, monkeypatch, gap_events):
    calls: list[tuple[list[str], dict]] = []
    ref = lambda day: {c: 10008.0 for c in _CODES}  # noqa: E731

    def _fake_backfill(dates, **kw):
        calls.append((list(dates), kw))
        return {
            "days": [
                {
                    "date": d,
                    "action": "written",
                    "rows": 240 * len(_CODES),
                    "etfs": len(_CODES),
                    "verified": True,
                }
                for d in dates
            ]
        }

    monkeypatch.setattr(em, "backfill", _fake_backfill)
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES, daily_ref=ref)

    assert len(calls) == 1
    dates, kw = calls[0]
    assert dates == [_DAY]
    assert kw["require_verified"] is True
    assert kw["daily_ref"] is ref
    assert kw["dry_run"] is False
    assert rep["ok"] is True
    assert rep["written_days"] == [_DAY]
    assert rep["attempts"] == 1
    assert rep["days"][0]["verified"] is True
    assert gap_events == []


def test_missing_daily_ref_falls_back_to_tq_daily_ref(iso_engine, monkeypatch):
    built: list[list[str]] = []
    ref = lambda day: {c: 1.0 for c in _CODES}  # noqa: E731

    def _fake_ref_factory(codes, **_kw):
        built.append(list(codes))
        return ref

    monkeypatch.setattr(em, "tq_daily_ref", _fake_ref_factory)
    seen: dict = {}

    def _fake_backfill(dates, **kw):
        seen.update(kw)
        return {"days": [{"date": d, "action": "written", "rows": 1, "etfs": len(_CODES),
                          "verified": True} for d in dates]}

    monkeypatch.setattr(em, "backfill", _fake_backfill)
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES)
    assert built == [_CODES]
    assert seen["daily_ref"] is ref
    assert seen["require_verified"] is True
    assert rep["ok"] is True


def test_source_has_no_reconcile_bypass_switch():
    src = inspect.getsource(gap)
    assert "--no-reconcile" not in src
    assert "require_verified=False" not in src
    params = inspect.signature(gap.run_gapfill).parameters
    assert not [p for p in params if "reconcile" in p]
    built_src = (_REPO / "src/bootstrap/startup.py").read_text(encoding="utf-8")
    assert "_gapfill.register(rt.scheduler.scheduler)" in built_src
    assert "_gapfill.start_startup_recovery(" in built_src


# ── 3. 有限窗口重试 ─────────────────────────────────────────────────────

def test_retry_then_success(iso_engine, monkeypatch):
    slept: list[float] = []
    replies = [
        {"days": [{"date": _DAY, "action": "skipped", "reason": "fetch_failed", "error": "timeout"}]},
        {"days": [{"date": _DAY, "action": "written", "rows": 720, "etfs": 3, "verified": True}]},
    ]
    monkeypatch.setattr(em, "backfill", lambda dates, **kw: replies.pop(0))
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES,
                          daily_ref=lambda d: {}, sleep=slept.append, max_attempts=2)
    assert slept == [gap.DEFAULT_RETRY_SLEEP_S]
    assert rep["attempts"] == 2
    assert rep["written_days"] == [_DAY]
    assert rep["ok"] is True


def test_retry_exhausted_is_failure_and_observed(iso_engine, monkeypatch, gap_events):
    slept: list[float] = []
    monkeypatch.setattr(
        em, "backfill",
        lambda dates, **kw: {"days": [{"date": _DAY, "action": "skipped",
                                       "reason": "unverified", "reconcile_error": "TQ 无该日"}]},
    )
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES,
                          daily_ref=lambda d: {}, sleep=slept.append, max_attempts=3)
    assert rep["attempts"] == 3
    assert slept == [gap.DEFAULT_RETRY_SLEEP_S] * 2
    assert rep["ok"] is False
    day = rep["days"][0]
    assert day["result"] == "failed" and day["reason"] == "unverified"
    assert len(gap_events) == 1 and gap_events[0][1] == "empty_source"
    assert gap_events[0][3]["attempts"] == 3


def test_non_retryable_failure_is_not_retried(iso_engine, monkeypatch):
    slept: list[float] = []
    calls: list[int] = []

    def _fake_backfill(dates, **kw):
        calls.append(1)
        return {"days": [{"date": _DAY, "action": "skipped", "reason": "out_of_window"}]}

    monkeypatch.setattr(em, "backfill", _fake_backfill)
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES,
                          daily_ref=lambda d: {}, sleep=slept.append, max_attempts=3)
    assert len(calls) == 1 and slept == []
    assert rep["attempts"] == 1 and rep["ok"] is False


def test_retry_stops_at_deadline(iso_engine, monkeypatch):
    """整轮 deadline 用尽后不再 sleep/重试（防止把调度线程拖死）。"""
    clock = iter([0.0, 1000.0, 2000.0, 3000.0, 4000.0])
    slept: list[float] = []
    monkeypatch.setattr(
        em, "backfill",
        lambda dates, **kw: {"days": [{"date": _DAY, "action": "skipped", "reason": "write_failed"}]},
    )
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES,
                          daily_ref=lambda d: {}, sleep=slept.append, max_attempts=5,
                          deadline_s=10.0, monotonic=lambda: next(clock))
    assert slept == []
    assert rep["attempts"] == 1
    assert rep["ok"] is False


def test_backfill_exception_is_retried_then_reported(iso_engine, monkeypatch, gap_events):
    """backfill 自己抛异常不能炸掉调度线程：按可重试失败处理，最终显式失败 + 上报。"""
    calls: list[int] = []

    def _boom(dates, **kw):
        calls.append(1)
        raise RuntimeError("db down")

    monkeypatch.setattr(em, "backfill", _boom)
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES,
                          daily_ref=lambda d: {}, sleep=lambda s: None, max_attempts=2)
    assert len(calls) == 2
    assert rep["ok"] is False
    day = rep["days"][0]
    assert day["result"] == "failed" and day["reason"] == "fetch_failed"
    assert "db down" in day["detail"]
    assert [e[1] for e in gap_events] == ["empty_source"]


# ── 4. 并发先到者：不覆盖、残缺即失败 ────────────────────────────────────

def test_race_where_other_writer_lands_partial_day_is_conflict(iso_engine, monkeypatch, gap_events):
    def _fake_backfill(dates, **kw):
        _insert(iso_engine, _DAY, _CODES[0], ["0931"], source="em_trends2")
        return {"days": [{"date": _DAY, "action": "skipped", "reason": "date_partially_ingested"}]}

    monkeypatch.setattr(em, "backfill", _fake_backfill)
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES, daily_ref=lambda d: {})
    day = rep["days"][0]
    assert day["result"] == "partial_conflict"
    assert rep["ok"] is False
    assert _count(iso_engine) == 1
    assert gap_events and gap_events[0][1] == "partial_conflict"


def test_race_where_other_writer_lands_complete_day_is_ok(iso_engine, monkeypatch):
    def _fake_backfill(dates, **kw):
        for c in _CODES:
            _insert(iso_engine, _DAY, c, em.SESSION_MINUTES, source="tdx_tq")
        return {"days": [{"date": _DAY, "action": "skipped", "reason": "date_already_ingested"}]}

    monkeypatch.setattr(em, "backfill", _fake_backfill)
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES, daily_ref=lambda d: {})
    assert rep["days"][0]["state"] == gap.ST_COMPLETE
    assert rep["ok"] is True
    assert _count(iso_engine) == 240 * len(_CODES)


# ── 5. 真链路：真实 em.backfill + 独立库（不覆盖、可幂等） ───────────────

def _trends_payload(code: str, day: str = "2026-11-06", *, price: float = 4.17, lots: int = 1000):
    lines = [f"{day} 09:30,0.000,{price},{price},{price},0,0.000,{price}"]
    for hm in em.SESSION_MINUTES:
        hhmm = f"{hm[:2]}:{hm[2:]}"
        yuan = f"{lots * 100 * price:.3f}"
        lines.append(f"{day} {hhmm},0.000,{price},{price},{price},{lots},{yuan},{price}")
    assert len(lines) == 241
    return {
        "rc": 0,
        "data": {"code": code.split(".")[0], "trends": lines},
    }


def test_real_backfill_writes_em_rows_then_second_run_is_idempotent(iso_engine, monkeypatch):
    monkeypatch.setattr(em, "_get_json", lambda code, **_kw: _trends_payload(code))
    expected = 240 * 1000 * 100 * 4.17 / 1e4  # 万元
    ref = lambda day: {c: expected for c in _CODES}  # noqa: E731
    gap.reset_state()

    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES, daily_ref=ref)
    assert rep["ok"] is True, rep
    assert rep["written_days"] == [_DAY]
    assert rep["days"][0]["verified"] is True
    assert _count(iso_engine, _DAY) == 240 * len(_CODES)
    with iso_engine.connect() as conn:
        sources = {r[0] for r in conn.execute(text("SELECT DISTINCT source FROM etf_minute_flow"))}
        minutes = {r[0] for r in conn.execute(text("SELECT DISTINCT minute FROM etf_minute_flow"))}
    assert sources == {"em_trends2"}
    assert minutes == set(em.SESSION_MINUTES) and "0930" not in minutes

    before = _count(iso_engine)
    rep2 = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES, daily_ref=ref)
    assert rep2["ok"] is True
    assert rep2["written_days"] == []
    assert rep2["days"][0]["state"] == gap.ST_COMPLETE
    assert _count(iso_engine) == before


# ── 6. 错过 15:35 的收盘定型：有限窗口补跑 ──────────────────────────────

def test_catch_up_close_reruns_when_today_empty(iso_engine, gap_events):
    calls: list[int] = []
    out = gap.catch_up_close(
        now=datetime(2026, 11, 10, 16, 50), engine=iso_engine, codes=_CODES,
        runner=lambda: calls.append(1) or {"ok": True, "rows": 720},
    )
    assert calls == [1]
    assert out["action"] == "rerun" and out["ok"] is True and out["rows"] == 720


def test_catch_up_close_window_and_state_guards(iso_engine):
    def _runner():
        raise AssertionError("不该重跑")

    early = gap.catch_up_close(now=datetime(2026, 11, 10, 15, 0), engine=iso_engine,
                               codes=_CODES, runner=_runner)
    assert early["reason"] == "too_early" and early["ok"] is True

    late = gap.catch_up_close(now=datetime(2026, 11, 10, 22, 30), engine=iso_engine,
                              codes=_CODES, runner=_runner)
    assert late["reason"] == "out_of_window" and late["ok"] is True

    holiday = gap.catch_up_close(now=datetime(2026, 10, 5, 17, 0), engine=iso_engine,
                                 codes=_CODES, runner=_runner)
    assert holiday["reason"] == "non_trading_day" and holiday["ok"] is True


def test_catch_up_close_skips_complete_day(iso_engine):
    def _runner():
        raise AssertionError("不该重跑")

    for c in _CODES:
        _insert(iso_engine, "20261110", c, em.SESSION_MINUTES, source="tdx_tq")
    done = gap.catch_up_close(now=datetime(2026, 11, 10, 16, 50), engine=iso_engine,
                             codes=_CODES, runner=_runner)
    assert done["reason"] == "already_complete" and done["ok"] is True


def test_catch_up_close_refuses_foreign_source_day(iso_engine, gap_events):
    """当日已有异源（东财/用户数据）时拒绝重跑 TQ，保持同日单源。"""
    def _runner():
        raise AssertionError("不该重跑")

    _insert(iso_engine, "20261110", _CODES[0], ["1500"], source="em_trends2")
    foreign = gap.catch_up_close(now=datetime(2026, 11, 10, 16, 50), engine=iso_engine,
                                codes=_CODES, runner=_runner)
    assert foreign["reason"] == "foreign_source" and foreign["ok"] is True
    assert _count(iso_engine, "20261110") == 1


def test_catch_up_close_reruns_partial_own_source_day(iso_engine):
    """当日只有源自己的残缺行（TQ 写了一半）→ 允许同源重跑补齐，不涉覆盖。"""
    _insert(iso_engine, "20261110", _CODES[0], ["0931"], source="tdx_tq")
    calls: list[int] = []
    out = gap.catch_up_close(
        now=datetime(2026, 11, 10, 16, 50), engine=iso_engine, codes=_CODES,
        runner=lambda: calls.append(1) or {"ok": True, "rows": 240},
    )
    assert calls == [1] and out["action"] == "rerun"
    assert _count(iso_engine, "20261110") == 1  # 本函数不写库，写入只由 run_close 负责


def test_catch_up_close_runner_failure_is_observed(iso_engine, gap_events):
    def _boom():
        raise RuntimeError("TQ 网关不可用")

    out = gap.catch_up_close(now=datetime(2026, 11, 10, 16, 50), engine=iso_engine,
                             codes=_CODES, runner=_boom)
    assert out["ok"] is False and out["reason"] == "close_runner_failed"
    assert gap_events and gap_events[0][1] == "close_catchup_failed"


# ── 7. 调度装配：只挂 cron，装配期零 I/O ────────────────────────────────

class _FakeScheduler:
    def __init__(self):
        self.jobs: list[tuple[str, dict]] = []

    def add_job(self, func, trigger, **kw):
        self.jobs.append((trigger, {"func": func, **kw}))


def test_register_wires_three_cron_jobs_without_io(monkeypatch):
    _no_backfill(monkeypatch)
    sched = _FakeScheduler()
    assert gap.register(sched) is True
    assert [j[1]["id"] for j in sched.jobs] == [
        "etf-minute-gapfill-am",
        "etf-minute-gapfill-close",
        "etf-minute-gapfill-late",
    ]
    times = [(j[1]["hour"], j[1]["minute"]) for j in sched.jobs]
    assert times == [(8, 40), (16, 50), (18, 50)]
    for trigger, kw in sched.jobs:
        assert trigger == "cron"
        assert kw["day_of_week"] == "mon-fri"
        assert kw["max_instances"] == 1
        assert kw["coalesce"] is True
        assert kw["replace_existing"] is True
        assert kw["misfire_grace_time"] == 900
    assert gap.register(None) is False


def test_startup_recovery_skips_trading_session(iso_engine, monkeypatch):
    monkeypatch.setattr(
        gap, "run_after_close",
        lambda **kw: (_ for _ in ()).throw(AssertionError("交易时段不该跑恢复")),
    )
    out = gap.startup_recovery(now=datetime(2026, 11, 10, 10, 0), engine=iso_engine)
    assert out["ok"] is True and out["reason"] == "in_session"


def test_startup_recovery_runs_outside_session(iso_engine, monkeypatch):
    seen: dict = {}

    def _fake_after_close(**kw):
        seen.update(kw)
        return {"ok": True, "close": {}, "gapfill": {}}

    monkeypatch.setattr(gap, "run_after_close", _fake_after_close)
    out = gap.startup_recovery(now=datetime(2026, 11, 10, 20, 0), engine=iso_engine)
    assert out["ok"] is True and seen["engine"] is iso_engine


def test_start_startup_recovery_is_non_blocking(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def _fake_recovery(**_kw):
        started.set()
        release.wait(5)
        finished.set()
        return {"ok": True}

    monkeypatch.setattr(gap, "startup_recovery", _fake_recovery)
    monkeypatch.setattr(gap, "_STARTED", False)

    t0 = time.monotonic()
    thread = gap.start_startup_recovery(delay_s=0)
    elapsed = time.monotonic() - t0

    assert thread is not None and elapsed < 0.5, f"启动恢复阻塞了调用方: {elapsed:.2f}s"
    assert started.wait(2)
    assert not finished.is_set(), "start_startup_recovery 必须立即返回，不能等恢复跑完"
    release.set()
    thread.join(5)
    assert finished.is_set()

    # 幂等：只允许起一次
    assert gap.start_startup_recovery(delay_s=0) is None


# ── 8. 可观测状态 ────────────────────────────────────────────────────────

def test_status_keeps_last_run_and_failure_surface(iso_engine, monkeypatch, gap_events):
    monkeypatch.setattr(
        em, "backfill",
        lambda dates, **kw: {"days": [{"date": _DAY, "action": "skipped", "reason": "fetch_failed"}]},
    )
    gap.reset_state()
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES,
                          daily_ref=lambda d: {}, max_attempts=1)
    st = gap.status()
    assert rep["ok"] is False
    assert st["run_seq"] == 1 and st["failed_days"] == [_DAY]
    assert st["days"][0]["reason"] == "fetch_failed"
    assert st["empty_source_days"] == [_DAY]
    assert gap.runs() == 1


def test_gap_event_really_lands_in_error_tracker_jsonl(tmp_path, monkeypatch):
    """空源事件必须真的进既有可观测面（error_tracker JSONL，/api/logs 读它）。"""
    target = tmp_path / "error_events.jsonl"
    monkeypatch.setenv("ERROR_TRACKER_FILE", str(target))
    gap._publish_gap_event("20261106", "empty_source", "fetch_failed: timeout", attempts=2)
    lines = target.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    import json

    rec = json.loads(lines[0])
    assert rec["type"] == "EtfMinuteSourceGap"
    assert "20261106" in rec["message"] and "empty_source" in rec["message"]
    assert rec["context"]["module"] == "etf_minute_gapfill"
    assert rec["context"]["attempts"] == 2


def test_survey_reports_gaps_older_than_em_window(iso_engine, gap_events):
    """超出东财 5 日窗口的 0 行日必须上报（补不到 ≠ 没缺口）。"""
    out = gap.survey_unfillable_gaps(now=_TODAY, engine=iso_engine, codes=_CODES)
    assert out["window_days"] == ["20261104", "20261105", "20261106", "20261109"]
    assert "20261028" in out["unfillable_days"]
    assert "20261109" not in out["unfillable_days"], "窗口内日期交给 backfill，不算 '补不到'"
    assert [e[1] for e in gap_events] == ["unfillable_gaps"]
    assert "20261028" in gap_events[0][3]["days"]


def test_survey_is_quiet_when_older_days_have_data(iso_engine, gap_events):
    for c in _CODES:
        _insert(iso_engine, "20261028", c, em.SESSION_MINUTES, source="tdx_tq")
    out = gap.survey_unfillable_gaps(now=_TODAY, engine=iso_engine, codes=_CODES, days=15)
    assert "20261028" not in out["unfillable_days"]
    for ev in gap_events:
        assert "20261028" not in ev[3]["days"]


# ── 9. 审计阻断：全篮子 20/20、旧残缺日、NULL 来源、engine 注入、窗口 ──

def test_complete_days_are_not_misreported_as_deferred(iso_engine, monkeypatch):
    """complete 是"已完成"，不是"排队中"：旧实现把它混进 deferred_days。"""
    for c in _CODES:
        _insert(iso_engine, _DAY, c, em.SESSION_MINUTES, source="tdx_tq")
    _no_backfill(monkeypatch)
    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_CODES)
    assert rep["ok"] is True
    assert rep["days"][0]["state"] == gap.ST_COMPLETE
    assert rep["complete_days"] == [_DAY]
    assert rep["deferred_days"] == []


def test_legacy_16_of_20_day_is_partial_and_never_overwritten(iso_engine, monkeypatch, gap_events):
    """旧 80% 闸门留下的 16/20 天：必须残缺 + 不覆盖 + 不调 backfill。"""
    for c in _BASKET[:16]:
        _insert(iso_engine, _DAY, c, em.SESSION_MINUTES, source="tdx_tq")
    before = _count(iso_engine)
    _no_backfill(monkeypatch)

    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_BASKET)
    day = rep["days"][0]
    assert day["state"] == gap.ST_PARTIAL
    assert day["result"] == "partial"
    assert day["codes"] == 16
    assert day["missing_codes"] == sorted(_BASKET[16:])
    assert rep["ok"] is False and rep["failed_days"] == [_DAY]
    assert rep["written_days"] == [] and rep["deferred_days"] == []
    assert _count(iso_engine) == before, "残缺日一行都不许动"
    assert [e[1] for e in gap_events] == ["partial_day"]


def test_null_source_day_is_partial_and_cannot_be_reclassified_as_tq(iso_engine, monkeypatch, gap_events):
    """NULL 来源不能并进 tdx_tq：要显式归为 unknown，且收盘定型拒绝重跑。"""
    for c in _BASKET[:-1]:
        _insert(iso_engine, _DAY, c, em.SESSION_MINUTES, source="tdx_tq")
    _insert(iso_engine, _DAY, _BASKET[-1], em.SESSION_MINUTES, source=None)
    _no_backfill(monkeypatch)

    rep = gap.run_gapfill([_DAY], engine=iso_engine, now=_TODAY, codes=_BASKET)
    day = rep["days"][0]
    assert day["state"] == gap.ST_PARTIAL
    assert day["null_source_rows"] == len(em.SESSION_MINUTES)
    assert "unknown" in day["sources"]
    assert "tdx_tq" in day["sources"]
    assert rep["ok"] is False
    assert gap_events and gap_events[0][1] == "partial_day"
    assert gap_events[0][3]["null_source_rows"] == len(em.SESSION_MINUTES)

    def _runner():
        raise AssertionError("来源不明/异源日不该重跑 TQ 收盘定型")

    close = gap.catch_up_close(now=datetime(2026, 11, 6, 16, 50), engine=iso_engine,
                               codes=_BASKET, runner=_runner)
    assert close["reason"] == "foreign_source" and close["ok"] is True
    assert close["null_source_rows"] == len(em.SESSION_MINUTES)


def test_run_gapfill_requires_injected_engine_and_never_imports_web(monkeypatch):
    """core 运行时不再自己找 web engine：未注入即显式报错。"""
    gap.configure_engine(None)
    monkeypatch.setattr(gap, "_engine_from_startup_modules", lambda: None)
    with pytest.raises(gap.EngineNotInjected):
        gap.run_gapfill([_DAY], now=_TODAY, codes=_CODES)

    src = inspect.getsource(gap)
    assert "_default_engine" not in src
    assert re.search(r"^\s*(from|import)\s+src\.web", src, re.M) is None


def test_configure_engine_is_the_startup_injection_seam(iso_engine, monkeypatch):
    """startup 注入的 provider 会在未显式传 engine 时被使用（同一实例）。"""
    seen: dict = {}

    def _fake_backfill(dates, **kw):
        seen.update(kw)
        return {"days": [{"date": d, "action": "written", "rows": 720, "etfs": len(_CODES),
                          "verified": True} for d in dates]}

    monkeypatch.setattr(em, "backfill", _fake_backfill)
    gap.configure_engine(lambda: iso_engine)
    try:
        rep = gap.run_gapfill([_DAY], now=_TODAY, codes=_CODES, daily_ref=lambda d: {})
    finally:
        gap.configure_engine(None)
    assert rep["ok"] is True
    assert seen["engine"] is iso_engine
    assert seen["require_verified"] is True


def test_in_session_normalizes_aware_utc():
    """aware UTC 的 02:00 = 北京 10:00，必须判为交易时段（旧实现按 UTC 钟点会漏）。"""
    from datetime import timezone

    assert gap._in_session(datetime(2026, 11, 10, 2, 0, tzinfo=timezone.utc)) is True
    assert gap._in_session(datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)) is False


def test_startup_recovery_skips_tq_close_window(iso_engine, monkeypatch):
    """15:35–16:30 是 TQ 收盘定型重负载窗口：启动恢复不并跑。"""
    monkeypatch.setattr(
        gap, "run_after_close",
        lambda **kw: (_ for _ in ()).throw(AssertionError("收盘定型窗口不该跑启动恢复")),
    )
    for hm in ((15, 45), (16, 0)):
        out = gap.startup_recovery(now=datetime(2026, 11, 10, hm[0], hm[1]), engine=iso_engine)
        assert out["ok"] is True and out["reason"] == "tq_close_window"
    edge = gap.startup_recovery(now=datetime(2026, 11, 10, 16, 30), engine=iso_engine)
    assert edge["reason"] == "tq_close_window"

    seen: dict = {}

    def _fake(**kw):
        seen.update(kw)
        return {"ok": True, "close": {}, "gapfill": {}}

    monkeypatch.setattr(gap, "run_after_close", _fake)
    out = gap.startup_recovery(now=datetime(2026, 11, 10, 16, 31), engine=iso_engine)
    assert out["ok"] is True and seen["engine"] is iso_engine
