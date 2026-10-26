"""会话去重（清理测试数据）回归。

安全红线：
  1. **默认只试运行**，不传 apply 一条都不许删；
  2. 每组至少保留 keep 条（keep≥1，不能把整组删空）；
  3. 只动本人会话，别人的同名会话不受影响；
  4. 无标题会话不参与（可能是刚落库还没命名）；
  5. 单次删除有上限，超了要如实报 truncated。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import Base, ChatConversation, ChatMessage
from src.web.api import copilot_workbench
from src.web.api.auth import get_current_user
from src.web.database import get_db


class _User:
    id = "u1"
    username = "tester"
    role = "member"
    permissions = {}


@pytest.fixture()
def env():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng, autoflush=False)

    db = Session()
    now = datetime.utcnow()
    # 同一标题 4 条（其中 3 条应被删），另有一条别人的同名会话，一条无标题
    for i in range(4):
        db.add(ChatConversation(
            user_id="u1", title="上证指数现在多少点",
            created_at=now - timedelta(minutes=i), updated_at=now - timedelta(minutes=i),
        ))
    db.add(ChatConversation(user_id="other", title="上证指数现在多少点",
                            created_at=now, updated_at=now))
    db.add(ChatConversation(user_id="u1", title="", created_at=now, updated_at=now))
    db.commit()
    # 给将被删的那条挂一条消息，验证级联清理
    first = db.query(ChatConversation).filter(ChatConversation.user_id == "u1").order_by(
        ChatConversation.id.asc()
    ).first()
    db.add(ChatMessage(conversation_id=first.id, role="user", content="hi"))
    db.commit()
    db.close()

    app = FastAPI()
    app.include_router(copilot_workbench.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: Session()
    app.dependency_overrides[get_current_user] = lambda: _User()
    return TestClient(app), Session


def test_默认只试运行不删数据(env):
    client, Session = env
    body = client.post("/api/copilot/conversations/dedupe", json={}).json()
    assert body["apply"] is False
    assert body["would_delete"] == 3
    assert body["groups"][0]["title"] == "上证指数现在多少点"
    assert body["groups"][0]["total"] == 4

    db = Session()
    try:
        assert db.query(ChatConversation).count() == 6  # 一条没少
    finally:
        db.close()


def test_apply后每组保留最新一条且消息一并清理(env):
    client, Session = env
    body = client.post("/api/copilot/conversations/dedupe", json={"apply": True}).json()
    assert body["deleted"] == 3

    db = Session()
    try:
        mine = db.query(ChatConversation).filter(ChatConversation.user_id == "u1").all()
        same = [c for c in mine if c.title == "上证指数现在多少点"]
        assert len(same) == 1                       # 每组留 1 条
        # 别人的同名会话不受影响
        assert db.query(ChatConversation).filter(ChatConversation.user_id == "other").count() == 1
        # 无标题的不参与
        assert any((c.title or "") == "" for c in mine)
        # 被删会话的消息也清了（不给孤儿消息）
        deleted_ids = set(body["deleted_ids"])
        assert db.query(ChatMessage).filter(
            ChatMessage.conversation_id.in_(deleted_ids)
        ).count() == 0
    finally:
        db.close()


def test_keep_2_保留两条(env):
    client, Session = env
    body = client.post("/api/copilot/conversations/dedupe",
                       json={"apply": True, "keep": 2}).json()
    assert body["deleted"] == 2 and body["groups"][0]["total"] == 4


def test_单次删除有上限且如实报truncated(env):
    client, Session = env
    body = client.post("/api/copilot/conversations/dedupe",
                       json={"apply": False, "max_delete": 1}).json()
    assert body["would_delete"] == 1
    assert body["truncated"] is True


def test_min_group_控制是否参与(env):
    client, _ = env
    body = client.post("/api/copilot/conversations/dedupe",
                       json={"min_group": 5}).json()
    assert body["would_delete"] == 0 and body["groups"] == []
