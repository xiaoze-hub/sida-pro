"""端到端（HTTP 层）：AI 提议改通知设置 → 待确认列表如实带旧值/新值 → 确认 → 落库+审计。

为什么要有这一层：`test_copilot_settings_loop.py` 是函数级逻辑契约，它证明不了
"前端真正拿到的 JSON 里有没有旧值/三态"，也证明不了 `ActionError.status_code`
经 FastAPI 之后还是 409。这里用真实 router + 真实依赖覆盖，走完整 HTTP 往返：

    actions.propose（模拟 AI 工具产生 pending）
        → GET  /copilot/actions           （前端确认面板读的就是它）
        → POST /copilot/actions/{id}/apply（用户点确认）
        → GET  /copilot/actions           （清了）+ DB/审计对账

2026-11-07 复审阻断项：确认面板必须拿到 `diff[].before_state` 与真实 before；模型
自带的 before 在 propose 阶段就被真实基线覆盖，不会漏到界面上。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import AppSettings, AuditLog, CopilotAction, User
from src.web.api import copilot as copilot_api
from src.web.api.auth import get_current_user
from src.web.database import get_db


class _User:
    """已登录的 owner（真实 DB 行在下面 seed，权限按行判定）。"""

    id = "owner"
    username = "owner"
    role = "owner"
    nickname = None
    avatar = None


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    for table in (User.__table__, AppSettings.__table__, AuditLog.__table__, CopilotAction.__table__):
        table.create(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    s.add_all([
        User(id="owner", username="owner", password_hash="x", role="owner"),
        AppSettings(key="notify_retry_attempts", value="2"),
    ])
    s.commit()
    s.close()

    app = FastAPI()
    app.include_router(copilot_api.router, prefix="/api")  # 与 src/web/app.py 一致：/api/copilot/*

    def _db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    async def _user():
        return _User()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    c = TestClient(app, raise_server_exceptions=False)
    c.session_factory = Session  # 测试侧对账用
    yield c
    engine.dispose()


def _propose(client, **payload):
    """模拟 AI 工具落一条 pending（真实 actions.propose，含真实基线固化）。"""
    from src.harness.actions import propose
    with client.session_factory() as s:
        item = propose(s, user_id="owner", kind="setting", payload=payload)
    return item


def test_setting_confirmation_roundtrip(client):
    item = _propose(client, key="notify_retry_attempts", value="3",
                    before="999", before_present=False)  # 模型虚构的基线

    # 1) 前端确认面板读到的就是这一条，旧值/新值/三态齐全且来自后端
    resp = client.get("/api/copilot/actions")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [r["id"] for r in rows] == [item["id"]]
    row = rows[0]
    assert row["kind"] == "setting"
    assert row["payload"]["key"] == "notify_retry_attempts"
    assert row["payload"]["before"] == "2"  # 不是模型说的 999
    assert row["payload"]["before_present"] is True
    assert row["diff"] == [{
        "field": "notify_retry_attempts",
        "before": "2",
        "after": "3",
        "change": "update",
        "before_state": "value",
        "immutable": True,
    }]
    assert row["can_rollback"] is False  # 设置动作不可撤销

    # 2) 确认（真实 HTTP，真实事务）
    applied = client.post(f"/api/copilot/actions/{item['id']}/apply")
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["status"] == "applied"
    assert "2" in body["result"] and "3" in body["result"]

    # 3) 列表清空 + 库里/审计里都对得上
    assert client.get("/api/copilot/actions").json() == []
    with client.session_factory() as s:
        assert s.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "3"
        detail = json.loads(
            s.query(AuditLog).filter_by(action="copilot_settings_update").one().detail
        )
        assert detail["action_id"] == item["id"]
        assert detail["before"] == "2" and detail["after"] == "3"


def test_setting_confirmation_conflict_is_409_over_http(client):
    item = _propose(client, key="notify_retry_attempts", value="3")
    with client.session_factory() as s:
        s.query(AppSettings).filter_by(key="notify_retry_attempts").one().value = "1"
        s.commit()

    resp = client.post(f"/api/copilot/actions/{item['id']}/apply")
    assert resp.status_code == 409, resp.text
    assert "已被修改" in resp.json()["detail"]

    with client.session_factory() as s:
        assert s.query(AppSettings).filter_by(key="notify_retry_attempts").one().value == "1"
        assert s.query(AuditLog).count() == 0
        assert s.query(CopilotAction).filter_by(id=item["id"]).one().status == "pending"


def test_absent_key_confirmation_shows_default_state(client):
    item = _propose(client, key="notify_quiet_hours", value="09:00-10:00")
    row = client.get("/api/copilot/actions").json()[0]
    assert row["diff"][0]["before_state"] == "absent"
    assert row["diff"][0]["change"] == "add"
    assert "默认值" in row["diff"][0]["before"]
    assert "关闭" not in row["diff"][0]["before"]

    assert client.post(f"/api/copilot/actions/{item['id']}/apply").status_code == 200
    with client.session_factory() as s:
        assert s.query(AppSettings).filter_by(key="notify_quiet_hours").one().value == "09:00-10:00"
