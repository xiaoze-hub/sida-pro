"""首页 AI 策展(`POST /api/dashboard/curate`)的模型授权与归属。

**根的钉**（修复前可直接复现）：
  `curate_today` 既没有 `get_current_user` 依赖，调用 `_get_ai_client` 时也不传 `user`
  → 走的是"无 user = 系统调用"分支。后果有两层：
    ① 权限绕过：`model_access.mode=deny_all` / `granted` 用户照样拿到 AI 排序，
       连 demo/guest 共享账号都能烧 AI 额度；
    ② 归属绕过：BYOK 用户的自有 key 被无视，一律记在平台系统模型（平台 key）账上，
       成本看板里也归不到人（`llm_usage.user_id` 为空）。

**契约**（与 `/chat`、`/copilot/model` 同一套 `src/core/ai_client` 口径）：
  - 缺身份 → 401；demo/guest → 403（零授权）；
  - `deny_all` → 403；`granted` 空列表 → 403；`granted` 里模型行全被删 → 403；
  - 请求带 `model_id`：必须存在且已授权，否则 403 —— **不静默换成别的模型**；
  - 正常路径：BYOK 用户走自己的 base_url/api_key，平台用户走授权/场景模型；
  - LLM 调用失败 → 只本地按原序兜底，**绝不换全局系统模型重试**。

本文件用真实 router + 真实 `_get_ai_client`（只把最外层 `AIClient.chat` 打桩），
断言的是"到底用了哪把 key / 哪个模型 / 记在谁名下"，而不是 mock 被调过。
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import llm_context
from src.core.ai_client import AIClient
from src.web.api import dashboard
from src.web.api.auth import get_current_user
from src.web.database import Base, get_db
from src.web.models import AIModel, AIService, User, UserAIService

CURATE = "/api/dashboard/curate"
# 平台模型 A：默认模型，也是"全局系统模型"（修复前的泄漏出口）
PLATFORM_DEFAULT_KEY = "sk-platform-default"
PLATFORM_GRANTED_KEY = "sk-platform-granted"
BYOK_KEY = "sk-byok-user"


# ── 夹具 ────────────────────────────────────────────────────────────
@pytest.fixture()
def env(monkeypatch):
    """内存 sqlite + 真实 dashboard router；AIClient.chat 打桩记录真实解析结果。"""
    from src.web import models  # noqa: F401  —— 注册全部 ORM 模型

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    session = Session()

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    state: dict = {"user": None, "override_user": True}
    app = FastAPI()
    app.include_router(dashboard.router, prefix="/api/dashboard")

    def _user():
        if state["user"] is None:
            raise HTTPException(status_code=401, detail="未登录")
        return state["user"]

    app.dependency_overrides[get_db] = _db
    if state["override_user"]:
        app.dependency_overrides[get_current_user] = _user

    calls: list[dict] = []

    async def fake_chat(self, system_prompt, user_content, images=None, temperature=0.4):
        calls.append(
            {
                "base_url": self.base_url,
                "api_key": self.api_key,
                "model": self.model,
                "scene": self.scene,
                "user_id": (llm_context.current() or {}).get("user_id"),
                "system_prompt": system_prompt,
            }
        )
        return "0|90|测试理由"

    monkeypatch.setattr(AIClient, "chat", fake_chat)

    # 平台模型 A(默认) / B(授权用)
    svc_a = AIService(name="平台A", base_url="https://platform-a.example/v1", api_key=PLATFORM_DEFAULT_KEY)
    session.add(svc_a)
    session.flush()
    model_a = AIModel(name="A", service_id=svc_a.id, model="model-a", is_default=True)
    svc_b = AIService(name="平台B", base_url="https://platform-b.example/v1", api_key=PLATFORM_GRANTED_KEY)
    session.add(svc_b)
    session.flush()
    model_b = AIModel(name="B", service_id=svc_b.id, model="model-b", is_default=False)
    session.add(model_a)
    session.add(model_b)
    session.commit()

    box = {
        "session": session,
        "Session": Session,
        "app": app,
        "calls": calls,
        "state": state,
        "model_a": model_a,
        "model_b": model_b,
        "client": TestClient(app, raise_server_exceptions=False),
    }
    yield box
    session.close()


def _bind_user(state, db, username="member", role="member", permissions=None):
    user = User(
        id=str(uuid.uuid4()),
        username=username,
        password_hash="x",
        role=role,
        permissions=permissions if permissions is not None else [],
        is_active=True,
        token_version=1,
    )
    db.add(user)
    db.commit()
    state["user"] = user
    return user


def _granted(ids):
    return {"model_access": {"mode": "granted", "model_ids": list(ids)}}


def _inherit():
    return {"model_access": {"mode": "inherit", "model_ids": []}}


def _deny_all():
    return {"model_access": {"mode": "deny_all", "model_ids": []}}


def _payload(model_id=None):
    body = {
        "candidates": [
            {"type": "alert", "symbol": "600519", "name": "贵州茅台", "signal": "提醒:跌破 1600"},
        ]
    }
    if model_id is not None:
        body["model_id"] = model_id
    return body


def _post(env, model_id=None):
    return env["client"].post(CURATE, json=_payload(model_id))


# ── 1. 缺身份 / 零授权账号 ──────────────────────────────────────────
def test_missing_identity_is_401_and_never_calls_model(env):
    """没有登录态 → 401，且一次模型调用都不能发生（不许当"系统调用"用全局模型）。"""
    env["client"].app.dependency_overrides.pop(get_current_user, None)
    res = _post(env)
    assert res.status_code == 401, res.text
    assert env["calls"] == []


@pytest.mark.parametrize(
    "username,role",
    [("demo", "member"), ("visitor", "guest")],
)
def test_demo_and_guest_are_denied(env, username, role):
    """demo/guest 零授权（与 get_model_for_scene ③ 一致），即使平台有默认模型。"""
    _bind_user(env["state"], env["session"], username=username, role=role)
    res = _post(env)
    assert res.status_code == 403, res.text
    assert env["calls"] == []


def test_deny_all_user_is_denied_and_no_model_call(env):
    """deny_all：403，且绝不回落到全局默认模型。"""
    _bind_user(env["state"], env["session"], permissions=_deny_all())
    res = _post(env)
    assert res.status_code == 403, res.text
    assert env["calls"] == []


# ── 2. 不同套餐（inherit / granted / granted 空 / deny_all）───────────
def test_owner_inherit_uses_platform_default(env):
    """owner（inherit）→ 平台默认模型，成本记在本人名下。"""
    user = _bind_user(env["state"], env["session"], username="owner", role="owner", permissions=_inherit())
    res = _post(env)
    assert res.status_code == 200, res.text
    assert len(env["calls"]) == 1
    call = env["calls"][0]
    assert call["model"] == "model-a"
    assert call["api_key"] == PLATFORM_DEFAULT_KEY
    # 归属：llm_usage 会读这个 contextvar，必须是真实调用者
    assert call["user_id"] == user.id


def test_granted_user_uses_granted_model_not_global_default(env):
    """granted=[B] → 用 B 的服务商 key，不能落到默认模型 A。"""
    _bind_user(env["state"], env["session"], permissions=_granted([env["model_b"].id]))
    res = _post(env)
    assert res.status_code == 200, res.text
    assert len(env["calls"]) == 1
    call = env["calls"][0]
    assert call["model"] == "model-b"
    assert call["api_key"] == PLATFORM_GRANTED_KEY


def test_granted_empty_list_is_denied(env):
    """granted 空列表 = 显式全禁 → 403。"""
    _bind_user(env["state"], env["session"], permissions=_granted([]))
    res = _post(env)
    assert res.status_code == 403, res.text
    assert env["calls"] == []


def test_granted_ids_pointing_at_deleted_models_fails_closed(env):
    """授权列表里的模型行全被删了 → 403（不许退回全局默认模型）。"""
    _bind_user(env["state"], env["session"], permissions=_granted([999999]))
    res = _post(env)
    assert res.status_code == 403, res.text
    assert env["calls"] == []


# ── 3. model_id 显式指定 ────────────────────────────────────────────
def test_model_id_outside_grant_is_forbidden(env):
    """granted=[B] 却要 A → 403，不静默替换成 B 也不给 A。"""
    _bind_user(env["state"], env["session"], permissions=_granted([env["model_b"].id]))
    res = _post(env, model_id=env["model_a"].id)
    assert res.status_code == 403, res.text
    assert env["calls"] == []


def test_unknown_model_id_fails_closed(env):
    """model_id 不存在 → 403（修复前会被当成"查不到"而回落默认模型）。"""
    _bind_user(env["state"], env["session"], permissions=_inherit())
    res = _post(env, model_id=999999)
    assert res.status_code == 403, res.text
    assert env["calls"] == []


def test_authorized_model_id_is_honored(env):
    """granted 且 model_id 在授权内 → 用这个模型。"""
    _bind_user(env["state"], env["session"], permissions=_granted([env["model_a"].id, env["model_b"].id]))
    res = _post(env, model_id=env["model_b"].id)
    assert res.status_code == 200, res.text
    assert len(env["calls"]) == 1
    assert env["calls"][0]["model"] == "model-b"
    assert env["calls"][0]["api_key"] == PLATFORM_GRANTED_KEY


# ── 4. BYOK 归属 ────────────────────────────────────────────────────
def test_byok_user_uses_own_key_not_platform(env):
    """BYOK 用户 → 自己的 base_url/api_key；平台默认模型一把都不能碰。"""
    user = _bind_user(env["state"], env["session"], permissions=_inherit())
    env["session"].add(
        UserAIService(
            user_id=user.id,
            name="我的服务",
            base_url="https://byok.example/v1",
            api_key=BYOK_KEY,
            models_json=json.dumps(
                [{"name": "deepseek-chat", "model": "byok-model", "is_default": True, "scene": "chat"}]
            ),
        )
    )
    env["session"].commit()

    res = _post(env)
    assert res.status_code == 200, res.text
    assert len(env["calls"]) == 1
    call = env["calls"][0]
    assert call["base_url"] == "https://byok.example/v1"
    assert call["api_key"] == BYOK_KEY
    assert call["model"] == "byok-model"
    assert call["user_id"] == user.id


def test_granted_user_with_byok_is_not_blocked(env):
    """granted + BYOK 并存：BYOK 自有 key 不算越权（granted 事后校验必须放行它）。"""
    user = _bind_user(env["state"], env["session"], permissions=_granted([env["model_b"].id]))
    env["session"].add(
        UserAIService(
            user_id=user.id,
            name="我的服务",
            base_url="https://byok.example/v1",
            api_key=BYOK_KEY,
            models_json=json.dumps([{"model": "byok-model", "is_default": True}]),
        )
    )
    env["session"].commit()

    res = _post(env)
    assert res.status_code == 200, res.text
    assert env["calls"][0]["api_key"] == BYOK_KEY


def test_ai_failure_keeps_local_fallback_without_switching_model(env, monkeypatch):
    """AI 抛错 → 本地按原序兜底，且只尝试过用户自己的那一次调用（不换全局模型重试）。"""
    user = _bind_user(env["state"], env["session"], permissions=_inherit())
    env["session"].add(
        UserAIService(
            user_id=user.id,
            name="我的服务",
            base_url="https://byok.example/v1",
            api_key=BYOK_KEY,
            models_json=json.dumps([{"model": "byok-model", "is_default": True}]),
        )
    )
    env["session"].commit()

    calls: list[str] = []

    async def boom(self, *a, **k):
        calls.append(self.api_key)
        raise RuntimeError("upstream down")

    monkeypatch.setattr(AIClient, "chat", boom)

    res = _post(env)
    assert res.status_code == 200, res.text
    body = res.json()
    assert [it["index"] for it in body["items"]] == [0]
    # 只用自己的 key 试了一次；平台默认模型的 key 从未出现
    assert calls == [BYOK_KEY]


def test_curate_prompt_is_compliance_guarded(env):
    """D3 合规护栏仍在（授权修复不能顺手丢掉它）。"""
    _bind_user(env["state"], env["session"], permissions=_inherit())
    res = _post(env)
    assert res.status_code == 200, res.text
    assert "不构成投资建议" in env["calls"][0]["system_prompt"]
