"""数智参谋 run 归属隔离（P2-13，2026-10-28）：NULL 无主 run 不得被普通账号读取。

**要收紧的洞**：`copilot_jobs.py` / `copilot_workbench.py` 四处归属判断写成
`row.user_id and row.user_id != user.id` —— `user_id` 为 NULL（或空串）时直接短路
放行，等于任何登录账号拿到 run_id 就能读别人的历史轨迹（question / status / 工具步骤），
`POST /runs/{id}/cancel` 还能把这条 run 的状态改掉。

**为什么会有 NULL**：`copilot_runs.user_id` 是 2026-10-20 才补的列
（`src/web/migrations.py` 的 `_migrate_copilot_run_user_id`），在那之前落库的轨迹全是 NULL。
这批是**合法旧数据**，策略是：不回填、不猜归属、不分配给任何人，只按 `role=owner`
放给平台 owner 做审计读。

红线（逐条钉住）：
  1. 他人 run：治理/并行详情/事件流/取消一律 404（不泄露存在性）；
  2. NULL 归属 run：普通账号 404，owner 可读，且"读"不得顺带改写 user_id；
  3. 自己的 run：四处行为不变（防过度收紧）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import Base, CopilotRun, CopilotStep
from src.web.api import copilot, copilot_jobs, copilot_workbench
from src.web.api.auth import get_current_user
from src.web.database import get_db

MEMBER = "u1"
OTHER = "u2"


class _User:
    def __init__(self, uid: str, role: str = "member") -> None:
        self.id = uid
        self.username = f"user-{uid}"
        self.role = role
        self.permissions: dict = {}


@pytest.fixture()
def env(monkeypatch):
    """一个内存库 + 可切换当前用户的 app（端点直接查 SessionLocal / get_db）。"""
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False)

    import src.db.session as session_mod

    monkeypatch.setattr(session_mod, "SessionLocal", Session)

    db = Session()
    # 自己的 run：三个端点都应正常
    db.add(CopilotRun(run_id="r-mine", user_id=MEMBER, recipe="deep_diagnosis",
                      question="我的诊断", status="ok", tool_calls=2,
                      created_at=datetime.utcnow()))
    # 别人的 run：一律 404
    db.add(CopilotRun(run_id="r-other", user_id=OTHER, recipe=None, question="别人的问题",
                      status="ok", created_at=datetime.utcnow()))
    # NULL 归属（2026-10-20 之前的合法旧数据）：普通账号不可读
    db.add(CopilotRun(run_id="r-legacy", user_id=None, recipe=None, question="旧轨迹",
                      status="ok", created_at=datetime.utcnow()))
    # 空串归属（迁移/脏数据可能留这种值）：与 NULL 同口径，不能因为 falsy 就放行
    db.add(CopilotRun(run_id="r-empty", user_id="", recipe=None, question="空归属",
                      status="queued", created_at=datetime.utcnow()))
    db.add(CopilotStep(run_id="r-legacy", seq=1, tool="get_kline", ok=True, elapsed_ms=12))
    db.commit()
    db.close()

    active = {"user": _User(MEMBER)}
    app = FastAPI()
    app.include_router(copilot.router, prefix="/api")
    app.include_router(copilot_workbench.router, prefix="/api")
    app.include_router(copilot_jobs.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: Session()
    app.dependency_overrides[get_current_user] = lambda: active["user"]
    copilot_jobs._TASKS.clear()
    client = TestClient(app)
    client.act_as = lambda uid, role="member": active.__setitem__("user", _User(uid, role))
    return client, Session


def _row(Session, run_id: str) -> CopilotRun:
    db = Session()
    try:
        return db.query(CopilotRun).filter(CopilotRun.run_id == run_id).one()
    finally:
        db.close()


# ── 基线：自己的 run 四处都能用（防过度收紧）──────────────────────────────
def test_自己的run_治理页可读(env):
    client, _ = env
    assert client.get("/api/copilot/runs/r-mine/governance").status_code == 200


def test_自己的run_并行详情可读(env):
    client, _ = env
    body = client.get("/api/copilot/parallel/r-mine")
    assert body.status_code == 200
    assert body.json()["run_id"] == "r-mine"


def test_自己的run_事件流可读(env):
    client, _ = env
    resp = client.get("/api/copilot/runs/r-mine/events")
    assert resp.status_code == 200
    assert "event: status" in resp.text


# ── 列表 / 详情（copilot.py 已按 user_id 严格过滤，这里只做回归锁定）─────
def test_列表只回本人run_不含NULL与他人(env):
    """`GET /copilot/runs` 一直是 `user_id == 本人` 的严格口径 —— 钉住，别被"共享"化。"""
    client, _ = env
    rows = client.get("/api/copilot/runs").json()
    assert [r["run_id"] for r in rows] == ["r-mine"]


def test_NULL归属run_详情对普通账号404(env):
    client, _ = env
    assert client.get("/api/copilot/runs/r-legacy").status_code == 404
    assert client.get("/api/copilot/runs/r-mine").status_code == 200


def test_新提交的run一律带归属_不会产生新的NULL(env, monkeypatch):
    """NULL 只可能来自 2026-10-20 之前的旧行；新提交必须是显式归属。"""
    client, Session = env

    async def _stub(**_kw):
        return 1

    monkeypatch.setattr(copilot_jobs, "run_copilot_question", _stub)
    body = client.post("/api/copilot/runs", json={"question": "上证现在多少点"}).json()
    assert body["status"] == "queued"
    assert _row(Session, body["run_id"]).user_id == MEMBER


# ── 他人 run：404（既有行为的回归，不能因为我改判据而松掉）──────────────
def test_他人run_治理页404(env):
    client, _ = env
    assert client.get("/api/copilot/runs/r-other/governance").status_code == 404


def test_他人run_并行详情404(env):
    client, _ = env
    assert client.get("/api/copilot/parallel/r-other").status_code == 404


def test_他人run_事件流与取消404(env):
    client, _ = env
    assert client.get("/api/copilot/runs/r-other/events").status_code == 404
    assert client.post("/api/copilot/runs/r-other/cancel").status_code == 404


# ── NULL 归属：普通账号一律 404（本次修复的核心）────────────────────────
def test_NULL归属run_治理页对普通账号404(env):
    client, _ = env
    assert client.get("/api/copilot/runs/r-legacy/governance").status_code == 404


def test_NULL归属run_并行详情对普通账号404(env):
    client, _ = env
    assert client.get("/api/copilot/parallel/r-legacy").status_code == 404


def test_NULL归属run_事件流对普通账号404(env):
    """事件流是"步骤回放"出口：放行等于把旧 run 的工具链暴露给任意登录账号。"""
    client, _ = env
    resp = client.get("/api/copilot/runs/r-legacy/events")
    assert resp.status_code == 404
    assert "get_kline" not in resp.text


def test_NULL归属run_普通账号取消不了且状态不被改写(env):
    """取消是**写**：NULL 放行时任何账号都能把别人的 run 标成 cancelled。"""
    client, Session = env
    assert client.post("/api/copilot/runs/r-empty/cancel").status_code == 404
    assert _row(Session, "r-empty").status == "queued", "未授权的取消不得改状态"


# ── NULL 归属：owner 可审计读，但读不改归属 ─────────────────────────────
def test_NULL归属run_owner可审计读(env):
    client, _ = env
    client.act_as("owner-1", role="owner")
    assert client.get("/api/copilot/runs/r-legacy/governance").status_code == 200
    assert client.get("/api/copilot/parallel/r-legacy").status_code == 200


def test_读NULL归属run_不得顺带回填归属(env):
    """合法旧数据不误分配：拒绝 + owner 读取都不许改 user_id。"""
    client, Session = env
    client.get("/api/copilot/runs/r-legacy/governance")
    client.get("/api/copilot/parallel/r-legacy")
    client.post("/api/copilot/runs/r-legacy/cancel")
    client.act_as("owner-1", role="owner")
    client.get("/api/copilot/runs/r-legacy/governance")
    assert _row(Session, "r-legacy").user_id is None


# ── owner 也不能读别人的 run（owner 不放宽"有主"资源）─────────────────
def test_owner也不能读他人有主run(env):
    client, _ = env
    client.act_as("owner-1", role="owner")
    assert client.get("/api/copilot/runs/r-other/governance").status_code == 404
    assert client.get("/api/copilot/parallel/r-other").status_code == 404
    assert client.get("/api/copilot/runs/r-other/events").status_code == 404
