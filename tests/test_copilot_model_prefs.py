"""用户级模型选择（工作台·模型选择器）回归。

三件事必须钉住：
  1. 选中后**真的生效** —— `get_model_for_scene(db,"chat",user)` 返回所选模型
     （这正是 chat.py 组装客户端时走的路径，所以 chat.py 一行都不用改）；
  2. **越权被拒** —— 选了授权外的模型 → 403 且不落库；deny_all 用户一个都选不了；
  3. 清除后**回到跟随场景绑定**，不是"记住 but ignored"。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import AIService, AIModel, Base, UserAiPref
from src.web.api import copilot_workbench
from src.web.api.auth import get_current_user
from src.web.database import get_db


class _User:
    def __init__(self, uid="u-pref", username="tester", role="member", permissions=None):
        self.id = uid
        self.username = username
        self.role = role
        self.permissions = permissions or {}


@pytest.fixture()
def env():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)

    db = Session()
    db.add(AIService(id=1, name="svc-a", base_url="http://a", api_key="k"))
    db.add(AIModel(id=1, name="模型一", model="m-one", service_id=1, is_default=True))
    db.add(AIModel(id=2, name="模型二", model="m-two", service_id=1, is_default=False))
    db.commit()
    db.close()

    state = {"user": _User()}
    app = FastAPI()
    app.include_router(copilot_workbench.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: Session()
    app.dependency_overrides[get_current_user] = lambda: state["user"]
    return TestClient(app), Session, state


def _resolve_chat_model(Session, user):
    from src.core.ai_client import get_model_for_scene

    db = Session()
    try:
        model = get_model_for_scene(db, "chat", user=user)
        return None if model is None else getattr(model, "model", None)
    finally:
        db.close()


def test_选择后解析器真的用所选模型(env):
    client, Session, state = env
    user = state["user"]

    assert _resolve_chat_model(Session, user) == "m-one"  # 默认

    r = client.put("/api/copilot/model", json={"model_id": 2})
    assert r.status_code == 200, r.text
    assert r.json()["current"]["source"] == "pref"
    assert r.json()["current"]["model"] == "m-two"

    # chat.py 走的正是这条路径，所以"选择生效"这件事在这里被证明
    assert _resolve_chat_model(Session, user) == "m-two"


def test_清除后回到场景绑定(env):
    client, Session, state = env
    user = state["user"]
    client.put("/api/copilot/model", json={"model_id": 2})
    assert _resolve_chat_model(Session, user) == "m-two"

    r = client.put("/api/copilot/model", json={"model_id": None})
    assert r.status_code == 200
    assert r.json()["current"]["source"] != "pref"
    assert _resolve_chat_model(Session, user) == "m-one"


def test_越权选模型被拒且不落库(env):
    client, Session, state = env
    state["user"] = _User(permissions={"model_access": {"mode": "granted", "model_ids": [1]}})
    user = state["user"]

    r = client.put("/api/copilot/model", json={"model_id": 2})
    assert r.status_code == 403
    assert "不在你可用的模型范围内" in r.json()["detail"]

    db = Session()
    try:
        assert db.query(UserAiPref).count() == 0
    finally:
        db.close()
    assert _resolve_chat_model(Session, user) == "m-one"


def test_deny_all_用户可选清单为空且选不了(env):
    client, Session, state = env
    state["user"] = _User(permissions={"model_access": {"mode": "deny_all"}})

    body = client.get("/api/copilot/model").json()
    assert body["models"] == []
    assert body["current"]["source"] == "none"
    assert client.put("/api/copilot/model", json={"model_id": 1}).status_code == 403


def test_访客不能选模型(env):
    client, Session, state = env
    state["user"] = _User(username="demo")
    assert client.put("/api/copilot/model", json={"model_id": 1}).status_code == 403


def test_历史脏偏好不会越权生效(env):
    """绕过 API 直接写库（旧数据/人工改库）也必须被解析器拦住。"""
    client, Session, state = env
    state["user"] = _User(permissions={"model_access": {"mode": "granted", "model_ids": [1]}})
    user = state["user"]

    db = Session()
    db.add(UserAiPref(user_id=user.id, scene="chat", model_id=2))
    db.commit()
    db.close()

    assert _resolve_chat_model(Session, user) == "m-one"


def test_可选清单带服务商与默认标记(env):
    client, Session, state = env
    body = client.get("/api/copilot/model").json()
    assert [m["id"] for m in body["models"]] == [1, 2]
    assert all(m["service"] == "svc-a" for m in body["models"])
    assert body["models"][0]["is_default"] is True
    # 凭证绝不出后端
    assert all("api_key" not in m and "base_url" not in m for m in body["models"])
