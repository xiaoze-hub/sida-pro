"""A3(2026-09-08 风险方案1.3)调度器选主回归: fail-closed + 租约丢失真停。

背景: 旧实现 Redis 不可用时"回退全员启动"→ WEB_WORKERS=2 每个实例都自认
leader, 定时 Agent 双跑(LLM 费用翻倍/通知重复); 租约被抢只打日志, 调度器
跑到进程重启。本文件锁定:
- Redis 不可用 → try_acquire 返回 False + leader_state()=="failed"(fail-closed);
- 锁在别人手里 → 合法让位 leader_state()=="standby";
- SIDA_SCHEDULER_SINGLE_INSTANCE=1 单实例口子仍放行;
- 租约丢失 → scheduler_registry 全部 pause(真停), 恢复/抢回自动 resume;
- 全仓 add_job 站点防并发参数一致(max_instances=1/coalesce/misfire_grace_time=300)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core import scheduler_registry
from src.core import scheduler_leader as sl

ROOT = Path(__file__).resolve().parent.parent


class _FakeRedis:
    """NX 选主语义的最小 Redis 替身。"""

    def __init__(self, value: bytes | None = None, *, set_result: bool = True,
                 exc: Exception | None = None):
        self.value = value
        self.set_result = set_result
        self.exc = exc
        self.expire_calls: list[int] = []

    def set(self, key, val, nx=False, ex=None):
        if self.exc:
            raise self.exc
        return self.set_result

    def get(self, key):
        if self.exc:
            raise self.exc
        return self.value

    def expire(self, key, ttl):
        if self.exc:
            raise self.exc
        self.expire_calls.append(ttl)


class _FakeSched:
    def __init__(self):
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self):
        self.pause_calls += 1

    def resume(self):
        self.resume_calls += 1


@pytest.fixture(autouse=True)
def _reset_leader_state():
    sl._acquired = False
    sl._state = "init"
    scheduler_registry.clear()
    yield
    sl._acquired = False
    sl._state = "init"
    scheduler_registry.clear()


# ── try_acquire 选主 ──────────────────────────────────────────────


def test_redis_unavailable_fail_closed(monkeypatch):
    """Redis 抛异常 → 绝不自认 leader(fail-closed), 状态 failed 供 /health 告警。"""
    monkeypatch.setattr(sl, "_client", lambda: (_ for _ in ()).throw(ConnectionError("down")))
    monkeypatch.setattr(sl, "ACQUIRE_RETRY_SECONDS", 0)
    ok, note = sl.try_acquire()
    assert ok is False
    assert "fail-closed" in note
    assert sl.leader_state() == "failed"


def test_other_owner_yields_standby(monkeypatch):
    """锁在别人手里 → 合法让位(standby), 不是 failed(不该触发告警)。"""
    monkeypatch.setattr(
        sl, "_client", lambda: _FakeRedis(value=b"other-host:1", set_result=False))
    monkeypatch.setattr(sl, "ACQUIRE_RETRY_SECONDS", 0)
    ok, note = sl.try_acquire()
    assert ok is False
    assert "选主失败" in note and "other-host:1" in note
    assert sl.leader_state() == "standby"


def test_single_instance_escape(monkeypatch):
    """SIDA_SCHEDULER_SINGLE_INSTANCE=1 单实例口子跳过选主直接启动。"""
    monkeypatch.delenv("SIDA_ENABLE_SCHEDULERS", raising=False)
    monkeypatch.setenv("SIDA_SCHEDULER_SINGLE_INSTANCE", "1")
    ok, _ = sl.try_acquire()
    assert ok is True
    assert sl.leader_state() == "leader"


def test_force_disable_standby(monkeypatch):
    monkeypatch.setenv("SIDA_ENABLE_SCHEDULERS", "0")
    ok, _ = sl.try_acquire()
    assert ok is False
    assert sl.leader_state() == "standby"


def test_acquire_success_starts_renewal(monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(sl, "_start_renewal", lambda r, wid: started.append(wid))
    monkeypatch.setattr(sl, "_client", lambda: _FakeRedis(set_result=True))
    ok, note = sl.try_acquire()
    assert ok is True and "选主成功" in note
    assert started == [sl.worker_id()]
    assert sl.leader_state() == "leader"


def test_reload_reentry_takes_own_stale_lock(monkeypatch):
    """dev reload 重入: 残留锁的 value 是本 worker id → 续期接管。"""
    wid = sl.worker_id()
    started: list[str] = []
    monkeypatch.setattr(sl, "_start_renewal", lambda r, w: started.append(w))
    monkeypatch.setattr(
        sl, "_client", lambda: _FakeRedis(value=wid.encode(), set_result=False))
    ok, note = sl.try_acquire()
    assert ok is True and "续期" in note
    assert started == [wid]
    assert sl.leader_state() == "leader"


# ── _renewal_once 续期/真停/恢复 ──────────────────────────────────


def test_renewal_once_still_mine_renews():
    r = _FakeRedis(value=sl.worker_id().encode())
    assert sl._renewal_once(r, sl.worker_id(), paused=False) is False
    assert r.expire_calls == [sl.TTL_SECONDS]


def test_renewal_once_lease_lost_pauses_and_stays_paused():
    """租约被抢 → 真停; 抢不回则保持 paused, 且不重复 pause。"""
    sched = _FakeSched()
    scheduler_registry.register("agent", sched)
    r = _FakeRedis(value=b"thief:2", set_result=False)
    assert sl._renewal_once(r, sl.worker_id(), paused=False) is True
    assert sched.pause_calls == 1 and sched.resume_calls == 0
    assert sl._renewal_once(r, sl.worker_id(), paused=True) is True
    assert sched.pause_calls == 1  # 已停过不再重复


def test_renewal_once_expired_reacquire_resumes():
    """键过期且无人接手 → NX 抢回 + resume(此前已真停)。"""
    sched = _FakeSched()
    scheduler_registry.register("agent", sched)
    r = _FakeRedis(value=None, set_result=True)
    assert sl._renewal_once(r, sl.worker_id(), paused=True) is False
    assert sched.resume_calls == 1


def test_renewal_once_redis_error_pauses():
    sched = _FakeSched()
    scheduler_registry.register("agent", sched)
    r = _FakeRedis(exc=ConnectionError("down"))
    assert sl._renewal_once(r, sl.worker_id(), paused=False) is True
    assert sched.pause_calls == 1


def test_renewal_once_recovery_resumes():
    """Redis 恢复且租约仍是自己的 → 续期 + resume。"""
    sched = _FakeSched()
    scheduler_registry.register("agent", sched)
    r = _FakeRedis(value=sl.worker_id().encode())
    assert sl._renewal_once(r, sl.worker_id(), paused=True) is False
    assert sched.resume_calls == 1
    assert r.expire_calls == [sl.TTL_SECONDS]


# ── add_job 防并发参数一致性(验收 grep) ────────────────────────────


# 7 个主调度器 + 4 个挂在其上的辅助 job 注册文件
SCHEDULER_FILES = [
    "src/core/scheduler.py",
    "src/core/report_scheduler.py",
    "src/core/context_scheduler.py",
    "src/core/paper_trading_scheduler.py",
    "src/core/price_alert_scheduler.py",
    "src/core/l2_ticks_scheduler.py",
    "src/core/kline_backfill_scheduler.py",
    "src/core/auction_pool.py",
    "src/core/kline_precache.py",
    "src/core/thsdk_board.py",
    "src/core/data_quality_sentinel.py",
]


@pytest.mark.parametrize("rel", SCHEDULER_FILES)
def test_add_job_params_consistent(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    n_jobs = text.count("add_job(")
    assert n_jobs >= 1, rel
    assert text.count("max_instances=1") >= n_jobs, rel
    assert text.count("coalesce=True") >= n_jobs, rel
    assert text.count("misfire_grace_time=300") >= n_jobs, rel


# ── /health 探针接线 ──────────────────────────────────────────────


def test_health_probe_uses_leader_state_and_gauge():
    text = (ROOT / "src/web/api/health.py").read_text(encoding="utf-8")
    assert "from src.core.scheduler_leader import leader_state" in text
    assert 'record_component_status("scheduler_leader"' in text


def test_scheduler_leader_gauge_records():
    pytest.importorskip("prometheus_client")
    from src.web.api.health import _metrics, _init_metrics, record_component_status

    _init_metrics()
    record_component_status("scheduler_leader", False)
    assert _metrics.COMPONENT_STATUS.labels(component="scheduler_leader")._value.get() == 0
    record_component_status("scheduler_leader", True)
    assert _metrics.COMPONENT_STATUS.labels(component="scheduler_leader")._value.get() == 1


def test_prometheus_rule_locks_scheduler_leader():
    """P4 双向锁: 规则里的 component="scheduler_leader" 与 health 探针一致。"""
    rules = (ROOT / "deploy/prometheus-rules.yml").read_text(encoding="utf-8")
    assert 'SidaSchedulerLeaderDown' in rules
    assert 'sida_health_component_status{component="scheduler_leader"} == 0' in rules
