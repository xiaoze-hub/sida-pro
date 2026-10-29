"""报告失败重试端点回归（2026-10-24，交接文档第 2 项）。

钉住的事实：
- (a) 本人失败记录 → 200 queued，且**只**派发一次后台任务（幂等入口，不重复烧 LLM）
- (b) 别人的记录 → 403；owner 可重试系统行(user_id=NULL)
- (c) 已成功的记录 → 409（不白跑一次）
- (d) 不存在的记录 → 404；agent 未启用 → 400

设计动机：失败态没有下一步就是死路 —— 用户看到「限流」只能干等。
重试接到 `trigger_agent`，`save_analysis` 按 (agent, symbol, date, user) upsert，
所以重复点击只会覆盖不会堆重复行；但入口这一层仍要拦「已成功」和「不是你的」。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import AgentConfig, AnalysisHistory, User
from src.web.api import agents as agents_mod
from src.web.api import auth, history as history_mod
from src.web.database import Base, get_db


@pytest.fixture()
def env(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    db = Session()
    owner = User(id=str(uuid.uuid4()), username="boss", password_hash="x", role="owner", is_active=True)
    member = User(id=str(uuid.uuid4()), username="bob", password_hash="x", role="member", is_active=True)
    other = User(id=str(uuid.uuid4()), username="eve", password_hash="x", role="member", is_active=True)
    db.add_all([owner, member, other])
    db.add_all(
        [
            AgentConfig(name="daily_report", display_name="盘后报告", kind="workflow", enabled=True),
            AgentConfig(name="paused_agent", display_name="停用的 Agent", kind="workflow", enabled=False),
        ]
    )
    db.commit()
    # id 必须在 db.close() 之前取: ORM 实例 detached 后访问属性会炸
    member_id, owner_id = member.id, owner.id

    def _row(
        *,
        user_id,
        agent_name="daily_report",
        status="failed",
        error="LLM 降级(owner): 限流",
        analysis_date="2026-10-23",
    ):
        # 每天一行的 UNIQUE(agent, symbol, date, user): 造数据必须给不同日期, 否则自身撞键
        return AnalysisHistory(
            user_id=user_id,
            agent_name=agent_name,
            stock_symbol="*",
            analysis_date=analysis_date,
            title="盘后报告",
            content="",
            status=status,
            error=error if status != "success" else None,
        )

    rows = {
        "member_failed": _row(user_id=member.id),
        "member_ok": _row(user_id=member.id, status="success", analysis_date="2026-10-22"),
        "other_failed": _row(user_id=other.id),
        "system_failed": _row(user_id=None),
        "paused": _row(user_id=member.id, agent_name="paused_agent", analysis_date="2026-10-21"),
    }
    db.add_all(list(rows.values()))
    db.commit()
    ids = {k: v.id for k, v in rows.items()}
    db.close()

    app = FastAPI()
    app.include_router(history_mod.router, prefix="/api")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    current = {"user_id": member_id}

    def _current_user():
        s = Session()
        try:
            return s.query(User).filter(User.id == current["user_id"]).first()
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[auth.get_current_user] = _current_user

    spawned: list[tuple] = []
    # 真实实现会起线程跑 LLM；这里只验证"派发了几次、派发给谁"
    monkeypatch.setattr(
        agents_mod,
        "_spawn_async_run",
        lambda fn, *args, name=None, notify_label=None: spawned.append((args, name, notify_label)),
    )

    return {
        "client": TestClient(app),
        "ids": ids,
        "member_id": member_id,
        "owner_id": owner_id,
        "current": current,
        "spawned": spawned,
    }


def test_member_retries_own_failed_record(env):
    """(a) 本人失败记录 → 200，且后台任务只派发一次。"""
    r = env["client"].post(f"/api/history/{env['ids']['member_failed']}/retry")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["queued"] is True
    assert body["agent_name"] == "daily_report"
    assert len(env["spawned"]) == 1
    args, name, notify = env["spawned"][0]
    assert args[0] == "daily_report"
    assert name == f"retry_history:{env['ids']['member_failed']}"
    assert notify == "盘后报告"


def test_cannot_retry_someone_elses_record(env):
    """(b) 别人的记录 → 403，不派发。"""
    r = env["client"].post(f"/api/history/{env['ids']['other_failed']}/retry")
    assert r.status_code == 403
    assert env["spawned"] == []


def test_owner_can_retry_system_row(env):
    """(b) 系统行(user_id=NULL)只有 owner 能重试。"""
    env["current"]["user_id"] = env["owner_id"]
    r = env["client"].post(f"/api/history/{env['ids']['system_failed']}/retry")
    assert r.status_code == 200, r.text
    assert len(env["spawned"]) == 1


def test_owner_cannot_retry_members_private_record(env):
    """owner 只能额外重试 NULL 系统行, 不能替 member 执行其私有报告。"""
    env["current"]["user_id"] = env["owner_id"]
    r = env["client"].post(f"/api/history/{env['ids']['member_failed']}/retry")
    assert r.status_code == 403
    assert env["spawned"] == []


def test_member_cannot_retry_system_row(env):
    """(b) 普通成员不能碰系统行。"""
    r = env["client"].post(f"/api/history/{env['ids']['system_failed']}/retry")
    assert r.status_code == 403
    assert env["spawned"] == []


def test_success_record_is_rejected(env):
    """(c) 已成功的记录 → 409，不白烧一次 LLM。"""
    r = env["client"].post(f"/api/history/{env['ids']['member_ok']}/retry")
    assert r.status_code == 409
    assert env["spawned"] == []


def test_missing_record_404(env):
    r = env["client"].post("/api/history/999999/retry")
    assert r.status_code == 404


def test_disabled_agent_rejected(env):
    """(d) 工作流 agent 被停用 → 400（先让用户去开启，别静默失败）。"""
    r = env["client"].post(f"/api/history/{env['ids']['paused']}/retry")
    assert r.status_code == 400
    assert "未启用" in r.json()["detail"]
    assert env["spawned"] == []
