"""Durable Run 运行时：状态机 / 心跳 / 僵尸接管（P0-0.1）。

背景：`copilot_runs.status` 没有心跳，进程重启后库里留下永久 `running` 的幽灵运行 ——
用户看到"正在分析"卡死，既不失败也不重试。这里钉住三件事：

1. **状态机**：终态不可回退（`ok → running` 必须被拒），兼容历史词汇 ok/degraded/error；
2. **接管**：心跳超 120s 的活跃 run 标 failed，且写清"可重试"给用户看懂；
3. **不自动重跑**：接管只改状态。重跑会烧额度、用户可能已看到半截答案 —— 由用户点。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from src.core import copilot_runtime as rt

Base = declarative_base()


class FakeRun(Base):
    __tablename__ = "copilot_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(36), nullable=False, unique=True)
    status = Column(String(16), nullable=False, default="running")
    error = Column(String(512), nullable=True)
    heartbeat_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)


@pytest.fixture()
def db(monkeypatch):
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    # 让运行时模块拿到这个假模型
    import src.db.models as models

    monkeypatch.setattr(models, "CopilotRun", FakeRun, raising=False)
    yield s
    s.close()


def _add(db, run_id, status="running", hb=None):
    db.add(FakeRun(run_id=run_id, status=status, heartbeat_at=hb))
    db.commit()


# ── 状态机 ────────────────────────────────────────────────────────────────
def test_终态不可回退():
    assert not rt.can_transition("ok", "running")
    assert not rt.can_transition("failed", "running")
    assert not rt.can_transition("cancelled", "running")
    # 同状态幂等
    assert rt.can_transition("ok", "ok")
    assert rt.can_transition("failed", "failed")


def test_历史词汇与计划书词汇都算终态():
    for s in ("ok", "degraded", "error", "succeeded", "failed", "cancelled"):
        assert rt.is_terminal(s), s
    for s in ("queued", "running", "paused"):
        assert rt.is_active(s), s
        assert not rt.is_terminal(s)


def test_合法迁移():
    assert rt.can_transition("queued", "running")
    assert rt.can_transition("queued", "failed")
    assert rt.can_transition("running", "ok")
    assert rt.can_transition("running", "degraded")
    assert rt.can_transition("running", "paused")
    assert rt.can_transition("paused", "running")
    # 不能从 queued 直接跳到 ok（没跑过不可能有结果）
    assert not rt.can_transition("queued", "ok")
    # 不能 running → queued（会丢进度）
    assert not rt.can_transition("running", "queued")


def test_空状态只允许入队或直接开跑():
    assert rt.can_transition(None, "queued")
    assert rt.can_transition(None, "running")
    assert not rt.can_transition(None, "ok")


# ── 接管 ──────────────────────────────────────────────────────────────────
def test_心跳超时的_running_被接管(db):
    now = datetime(2026, 10, 25, 14, 0, 0)
    _add(db, "dead", "running", hb=now - timedelta(seconds=300))
    _add(db, "alive", "running", hb=now - timedelta(seconds=10))

    reaped = rt.reap_stale_runs(db, now=now)

    assert reaped == ["dead"]
    dead = db.query(FakeRun).filter_by(run_id="dead").one()
    assert dead.status == "failed"
    assert dead.finished_at == now
    assert "可点「重试」" in (dead.error or "")
    alive = db.query(FakeRun).filter_by(run_id="alive").one()
    assert alive.status == "running", "心跳正常的不能动"


def test_没有心跳列的老数据_视为超时(db):
    """老记录从没写过心跳，说明写它的进程早就不在了 —— 正是要收的尸。"""
    now = datetime(2026, 10, 25, 14, 0, 0)
    _add(db, "legacy", "running", hb=None)
    assert rt.reap_stale_runs(db, now=now) == ["legacy"]


def test_排队中的没心跳不算死(db):
    """queued 本来就没开始跑，不能因为"没心跳"就判死。"""
    now = datetime(2026, 10, 25, 14, 0, 0)
    _add(db, "waiting", "queued", hb=None)
    assert rt.reap_stale_runs(db, now=now) == []
    assert db.query(FakeRun).filter_by(run_id="waiting").one().status == "queued"


def test_终态不被接管(db):
    now = datetime(2026, 10, 25, 14, 0, 0)
    for i, st in enumerate(("ok", "degraded", "error", "failed", "cancelled")):
        _add(db, f"done{i}", st, hb=now - timedelta(hours=1))
    assert rt.reap_stale_runs(db, now=now) == []


def test_dry_run_只报不改(db):
    now = datetime(2026, 10, 25, 14, 0, 0)
    _add(db, "zombie", "running", hb=now - timedelta(seconds=600))
    assert rt.reap_stale_runs(db, now=now, dry_run=True) == ["zombie"]
    assert db.query(FakeRun).filter_by(run_id="zombie").one().status == "running"


def test_接管阈值可配置(db):
    now = datetime(2026, 10, 25, 14, 0, 0)
    _add(db, "slow", "running", hb=now - timedelta(seconds=90))
    assert rt.reap_stale_runs(db, now=now) == [], "90s < 默认 120s 不该收"
    assert rt.reap_stale_runs(db, now=now, stale_after_s=60) == ["slow"]


def test_paused_超时也接管(db):
    now = datetime(2026, 10, 25, 14, 0, 0)
    _add(db, "p", "paused", hb=now - timedelta(seconds=400))
    assert rt.reap_stale_runs(db, now=now) == ["p"]


# ── 心跳 ──────────────────────────────────────────────────────────────────
def test_心跳刷新(db):
    now = datetime(2026, 10, 25, 14, 0, 0)
    _add(db, "r1", "running", hb=None)
    assert rt.heartbeat(db, "r1", now=now) is True
    assert db.query(FakeRun).filter_by(run_id="r1").one().heartbeat_at == now


def test_终态不再刷心跳_返回_False_不抛(db):
    now = datetime(2026, 10, 25, 14, 0, 0)
    _add(db, "r2", "ok", hb=None)
    assert rt.heartbeat(db, "r2", now=now) is False


def test_心跳对不存在的_run_返回_False(db):
    assert rt.heartbeat(db, "nope") is False
