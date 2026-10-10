"""决策账本回填接线(P0-1 审计修复, 2026-10-10) 的钉子。

背景: `decision_log.backfill_outcomes` 此前全仓无生产调度(仅测试调用, 生产引用只剩一条
注释) → DecisionLedger 直读的 ret_t1/hit_t1 列**从不被回填**, 命中率恒显『样本不足』,
"信号 → 结果" 反馈环整段死。本文件锁死接线:

  ① cron 注册存在(交易日 18:35, 复用传入的现有调度器, 不新开) + 非交易日跳过;
  ② 回填幂等: 重跑不双计(已填满的行不再进 pending);
  ③ 回填后 `stats()` 的 ret_t1/hit_t1 真的变非空(假行情序列构造 T+1/3/5);
  ④ 作业返回体 `ok=False` → 作业 failed; 抛异常 → failed(诚实性约定, 永不静默成功)。

禁令: 断言强度不得降低; 测试不触真实网络(行情序列用假 provider, 作业线程换同步替身)。
"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from src.core import decision_log as dl

ROOT = Path(__file__).resolve().parent.parent


class _ImmediateThread:
    """同步替身: .start() 直接在当前线程跑 target —— 消灭 job 测试的时序竞态。"""

    def __init__(self, target=None, name=None, daemon=None, kwargs=None, **_kw):
        self._target = target
        self._kwargs = kwargs or {}

    def start(self) -> None:
        if self._target is not None:
            self._target(**self._kwargs)


@pytest.fixture()
def env(monkeypatch):
    """内存库(decision_log + app_jobs 两表) + 同步线程 + 引擎指向测试库。"""
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        from src.web.migrations import _m165_app_jobs_table, _m175_decision_log

        _m165_app_jobs_table(conn)
        _m175_decision_log(conn)
    import src.db.session as dbs

    monkeypatch.setattr(dbs, "engine", eng)  # 作业框架 _save 读的就是它
    monkeypatch.setattr(dbs, "get_write_engine", lambda: eng)  # runner 解析写库
    monkeypatch.setattr(threading, "Thread", _ImmediateThread)
    yield eng


def _provider(bars: dict[str, list[tuple[str, float]]]):
    """假 K 线序列提供者: 按 (symbol, start_day) 给序列(与既有决策日志测试同口径)。"""

    def _p(_engine, symbol: str, start_day: str):
        return [b for b in bars.get(symbol, []) if b[0] >= start_day]

    return _p


_FULL_BARS = {
    "AAA": [
        ("2026-09-10", 10.0),
        ("2026-09-11", 11.0),  # T+1 → +10% 命中
        ("2026-09-12", 10.0),
        ("2026-09-15", 9.0),   # T+3 → -10% 未命中
        ("2026-09-16", 10.0),
        ("2026-09-17", 12.0),  # T+5 → +20% 命中
    ]
}


# ────────────────────────── ① cron 注册 / 交易日守卫 ──────────────────────────


def test_register_cron_schedule_and_reuse():
    """交易日 18:35 挂到传入的现有调度器(不新开); id/防并发参数一致。"""
    sched = Mock()
    assert dl.register_cron(sched) is True
    sched.add_job.assert_called_once()
    args = sched.add_job.call_args[0]
    kw = sched.add_job.call_args[1]
    assert args[0] is dl.backfill_daily_job and args[1] == "cron"
    assert kw["day_of_week"] == "mon-fri"
    assert kw["hour"] == 18 and kw["minute"] == 35
    assert kw["id"] == "decision-backfill-daily"
    assert kw["replace_existing"] is True
    assert kw["max_instances"] == 1 and kw["coalesce"] is True


def test_register_cron_none_safe():
    """传入 None / 无 add_job → 返回 False, 不崩。"""
    assert dl.register_cron(None) is False
    assert dl.register_cron(object()) is False


def test_startup_wires_decision_backfill_cron():
    """lifespan 里真的接了线(防"改了 core 忘了注册")。"""
    text = (ROOT / "src/bootstrap/startup.py").read_text(encoding="utf-8")
    assert "from src.core.decision_log import register_cron" in text
    assert "决策账本回填" in text


def test_daily_job_skips_non_trading_day(monkeypatch):
    """非交易日: 跳过, **绝不起回填作业**。"""
    from src.core import trading_calendar as tc

    monkeypatch.setattr(tc, "is_trading_day", lambda d: False)
    spawned = {"n": 0}
    monkeypatch.setattr(dl, "spawn_backfill", lambda **k: spawned.__setitem__("n", spawned["n"] + 1))
    out = dl.backfill_daily_job()
    assert out["skipped"] is True and out["ok"] is True
    assert spawned["n"] == 0


def test_daily_job_spawns_on_trading_day(monkeypatch):
    """交易日: 起作业(reason=scheduled)。"""
    from src.core import trading_calendar as tc

    monkeypatch.setattr(tc, "is_trading_day", lambda d: True)
    monkeypatch.setattr(dl, "spawn_backfill", lambda **k: {"started": True, "job_id": "j1", **k})
    out = dl.backfill_daily_job()
    assert out["started"] is True and out["job_id"] == "j1" and out["reason"] == "scheduled"


# ────────────────────────── ② 幂等重跑不双计 ──────────────────────────


def test_backfill_idempotent_repeat_no_double_count(env):
    dl.record_signal(env, signal_kind="resonance3", symbol="AAA", trade_date="2026-09-10", price=10.0)
    prov = _provider(_FULL_BARS)
    first = dl.backfill_outcomes(env, series_provider=prov)
    assert first == {"scanned": 1, "filled": 1}
    stat1 = dl.stats(env, min_sample=1)

    second = dl.backfill_outcomes(env, series_provider=prov)
    assert second == {"scanned": 0, "filled": 0}, "已填满的行不再进 pending → 重跑不双计"
    stat2 = dl.stats(env, min_sample=1)
    assert stat1["rows"][0]["n_total"] == stat2["rows"][0]["n_total"] == 1
    for label in ("t1", "t3", "t5"):
        assert stat1["rows"][0]["horizons"][label]["n"] == stat2["rows"][0]["horizons"][label]["n"] == 1


# ────────────────────────── ③ 回填后 stats 真的非空 ──────────────────────────


def test_backfill_fills_stats_columns(env):
    dl.record_signal(env, signal_kind="resonance3", symbol="AAA", trade_date="2026-09-10", price=10.0)
    before = dl.stats(env, min_sample=1)["rows"][0]["horizons"]
    assert before["t1"]["n"] == 0 and before["t1"]["hit_rate"] is None, "回填前必须『样本不足』"

    out = dl.backfill_outcomes(env, series_provider=_provider(_FULL_BARS))
    assert out["filled"] == 1

    with env.connect() as conn:
        r = conn.execute(
            sa.text("SELECT ret_t1, hit_t1, ret_t3, hit_t3, ret_t5, hit_t5 FROM decision_log")
        ).fetchone()
    assert r[0] is not None and int(r[1]) == 1
    assert r[2] is not None and int(r[3]) == 0
    assert r[4] is not None and int(r[5]) == 1

    after = dl.stats(env, min_sample=1)["rows"][0]["horizons"]
    for label in ("t1", "t3", "t5"):
        assert after[label]["n"] == 1
        assert after[label]["hit_rate"] is not None
        assert after[label]["insufficient"] is False
    assert after["t1"]["hit_rate"] == 1.0
    assert after["t3"]["hit_rate"] == 0.0
    assert after["t5"]["hit_rate"] == 1.0


# ────────────────────────── ④ 作业诚实性 / 单飞 ──────────────────────────


def test_backfill_job_succeeds_on_normal_counts(env, monkeypatch):
    from src.core.jobs import jobs

    monkeypatch.setattr(dl, "backfill_outcomes", lambda eng, **k: {"scanned": 3, "filled": 1})
    out = dl.spawn_backfill(reason="manual")
    assert out["started"] is True
    row = jobs.get(out["job_id"])
    assert row["status"] == "succeeded" and row["progress"] == 100


def test_backfill_job_failed_when_result_ok_false(env, monkeypatch):
    """返回体显式 ok=False → 作业 failed(v0.13.49 诚实性约定), 原因落 message/error。"""
    from src.core.jobs import jobs

    monkeypatch.setattr(dl, "backfill_outcomes", lambda eng, **k: {"ok": False, "reason": "回填源全空"})
    out = dl.spawn_backfill(reason="manual")
    row = jobs.get(out["job_id"])
    assert row["status"] == "failed"
    assert "回填源全空" in row["error"] and "回填源全空" in row["message"]


def test_backfill_job_failed_on_exception(env, monkeypatch):
    """回填本身抛异常 → 作业 failed, 且绝不自旋抛给调度器。"""
    from src.core.jobs import jobs

    def _boom(eng, **k):
        raise RuntimeError("boom-db")

    monkeypatch.setattr(dl, "backfill_outcomes", _boom)
    out = dl.spawn_backfill(reason="manual")
    row = jobs.get(out["job_id"])
    assert row["status"] == "failed" and "boom-db" in row["error"]


def test_spawn_single_flight_reuses_active_job(env):
    """同类活跃作业存在 → 复用同一 job_id(started=False), 不并发起第二个。"""
    from src.core.jobs import jobs

    jid, _ = jobs.create(dl.BACKFILL_JOB_KIND, "决策账本回填")  # 手动占位(活跃)
    out = dl.spawn_backfill(reason="manual")
    assert out["started"] is False and out["job_id"] == jid
    jobs.succeed(jid)


# ────────────────────────── 手动口子: POST /api/decisions/backfill ──────────────────────────


def test_api_backfill_route_registered():
    from src.web.api.decisions import router

    paths = {}
    for r in router.routes:
        paths.setdefault(r.path, set()).update(getattr(r, "methods", set()) or set())
    assert "POST" in paths.get("/backfill", set())


def test_api_backfill_endpoint_returns_job_id(monkeypatch):
    from src.web.api import decisions as api

    monkeypatch.setattr(
        dl, "spawn_backfill",
        lambda **k: {"started": True, "running": True, "reason": None, "job_id": "api-1", **k},
    )
    out = api.decisions_backfill(limit=250, _=None)
    assert out["job_id"] == "api-1" and out["started"] is True
