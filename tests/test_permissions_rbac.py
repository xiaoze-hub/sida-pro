"""SIDA RBAC 权限体系测试(2026-08-15)。

覆盖:
  1. 角色→权限映射(permissions.py): owner 全量 / member 浏览+操作 / guest 只读
  2. LLM 用户级解析(get_model_for_scene): demo 零授权 / BYOK 优先 /
     平台授权(inherit/granted/deny_all) / 无 user 系统调用走全局
  3. 中间件角色权限驱动: demo 只读+管理区403 / member 管理区403+可浏览例外 /
     owner 全过 / users.permissions 白名单通道
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from src.core.ai_client import get_model_for_scene
from src.core.permissions import (
    ALL_PERMISSIONS,
    GUEST_STRATEGY,
    MANAGE_PERMISSIONS,
    get_role_permissions,
    has_permission,
)


# ───────────────────────── 1. 角色映射 ─────────────────────────────


def test_owner_has_all_permissions():
    """owner 拥有全部权限点。"""
    assert get_role_permissions("owner") == set(ALL_PERMISSIONS)
    for p in ALL_PERMISSIONS:
        assert has_permission("owner", p)


def test_member_view_and_actions_but_no_manage():
    """member: 浏览全部 + 自选/持仓/预测/聊天/上传, 无 manage_*。"""
    perms = get_role_permissions("member")
    for p in (
        "view_dashboard", "view_quotes", "view_forecast", "view_reports",
        "view_opportunities", "edit_watchlist", "edit_portfolio",
        "run_prediction", "use_chat", "upload_files",
    ):
        assert p in perms
    for p in MANAGE_PERMISSIONS:
        assert p not in perms


def test_guest_view_only():
    """guest: 仅浏览, 无 manage_*/edit/run/use/upload。"""
    perms = get_role_permissions("guest")
    for p in ("view_dashboard", "view_quotes", "view_forecast", "view_reports", "view_opportunities"):
        assert p in perms
    for p in MANAGE_PERMISSIONS | {
        "edit_watchlist", "edit_portfolio", "run_prediction", "use_chat", "upload_files",
    }:
        assert p not in perms


def test_guest_strategy_constants():
    """guest 附加限制常量。"""
    assert GUEST_STRATEGY == {"watchlist_limit": 1, "get_hourly_limit": 20}


def test_unknown_role_denies_all():
    """未知角色/空角色 → 空权限(最严)。"""
    assert get_role_permissions(None) == set()
    assert get_role_permissions("hacker") == set()
    assert has_permission("hacker", "view_dashboard") is False


# ──────────────────── 2. get_model_for_scene 用户级解析 ─────────────


@pytest.fixture()
def scene_db():
    """独立内存 sqlite, 不污染真实 data/panwatch.db。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from src.web import models  # noqa: F401  注册所有 ORM 模型
    from src.web.database import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


def _mk_user(db, username="member_a", role="member", permissions=None):
    from src.web.models import User

    u = User(
        id=str(uuid.uuid4()),
        username=username,
        password_hash="x",
        role=role,
        permissions=permissions if permissions is not None else [],
    )
    db.add(u)
    db.commit()
    return u


def _mk_platform_model(db, name="DeepSeek", model="deepseek-chat", is_default=True):
    from src.web.models import AIModel, AIService

    svc = AIService(name=name, base_url="https://api.deepseek.com", api_key="sk-platform")
    db.add(svc)
    db.flush()
    m = AIModel(name=name, service_id=svc.id, model=model, is_default=is_default)
    db.add(m)
    db.commit()
    return m


def _mk_byok(db, user, base_url="https://api.deepseek.com/v1", api_key="sk-user", models=None):
    from src.web.models import UserAIService

    svc = UserAIService(
        user_id=user.id,
        name="我的DeepSeek",
        base_url=base_url,
        api_key=api_key,
        models_json=json.dumps(models or [
            {"name": "deepseek-chat", "model": "deepseek-chat", "is_default": True, "scene": ""},
        ]),
    )
    db.add(svc)
    db.commit()
    return svc


def test_model_scene_demo_returns_none(scene_db):
    """demo 用户(username==demo)零授权, 即使平台有模型。"""
    _mk_platform_model(scene_db)
    demo = _mk_user(scene_db, username="demo", role="member")
    assert get_model_for_scene(scene_db, "chat", user=demo) is None


def test_model_scene_guest_role_returns_none(scene_db):
    """role==guest 用户零授权。"""
    _mk_platform_model(scene_db)
    guest = _mk_user(scene_db, username="visitor", role="guest")
    assert get_model_for_scene(scene_db, "chat", user=guest) is None


def test_model_scene_demo_ignores_byok(scene_db):
    """demo 即使配了 BYOK 也返回 None(公共账号防泄露)。"""
    demo = _mk_user(scene_db, username="demo", role="member")
    _mk_byok(scene_db, demo)
    assert get_model_for_scene(scene_db, "chat", user=demo) is None


def test_model_scene_member_no_auth_returns_none(scene_db):
    """无 BYOK + 无 model_access + 模型池为空 → None。"""
    member = _mk_user(scene_db, username="plain_member")
    assert get_model_for_scene(scene_db, "chat", user=member) is None


def test_model_scene_inherit_member_gets_global(scene_db):
    """mode=inherit → 走全局解析逻辑(场景绑定/默认/第一个)。"""
    m = _mk_platform_model(scene_db)
    member = _mk_user(
        scene_db, username="inherit_member",
        permissions={"model_access": {"mode": "inherit", "model_ids": []}},
    )
    result = get_model_for_scene(scene_db, "chat", user=member)
    assert result is not None
    assert result.id == m.id


def test_model_scene_inherit_respects_scene_binding(scene_db):
    """inherit 模式下场景绑定仍然优先。"""
    from src.web.models import AIModel, AISceneBinding, AIService

    _mk_platform_model(scene_db)  # 默认模型兜底
    svc2 = AIService(name="S2", base_url="https://s2", api_key="k2")
    scene_db.add(svc2)
    scene_db.flush()
    m_bound = AIModel(name="B2", service_id=svc2.id, model="b2", is_default=False)
    scene_db.add(m_bound)
    scene_db.flush()
    scene_db.add(AISceneBinding(scene="reports", model_id=m_bound.id))
    scene_db.commit()

    member = _mk_user(
        scene_db, username="bind_member",
        permissions={"model_access": {"mode": "inherit", "model_ids": []}},
    )
    result = get_model_for_scene(scene_db, "reports", user=member)
    assert result.id == m_bound.id


def test_model_scene_granted_allows_listed(scene_db):
    """mode=granted 且场景模型在 model_ids → 放行。"""
    m = _mk_platform_model(scene_db)
    member = _mk_user(
        scene_db, username="granted_member",
        permissions={"model_access": {"mode": "granted", "model_ids": [m.id]}},
    )
    result = get_model_for_scene(scene_db, "chat", user=member)
    assert result is not None and result.id == m.id


def test_model_scene_granted_picks_from_list(scene_db):
    """granted 场景模型不在列表 → 从授权列表挑模型(2026-08-16 修复)。

    旧逻辑: 场景默认模型 A 不在授权 [B] → 全场景 None(授权了也用不了)。
    新逻辑: 返回授权列表内的 B —— owner 授权什么, 用户就能用什么。
    """
    from src.web.models import AIModel, AIService

    m_default = _mk_platform_model(scene_db, name="A", model="model-a", is_default=True)
    svc = AIService(name="S-B", base_url="https://sb", api_key="kb")
    scene_db.add(svc)
    scene_db.flush()
    m_b = AIModel(name="B", service_id=svc.id, model="model-b", is_default=False)
    scene_db.add(m_b)
    scene_db.commit()

    member = _mk_user(
        scene_db, username="granted_pick",
        permissions={"model_access": {"mode": "granted", "model_ids": [m_b.id]}},
    )
    result = get_model_for_scene(scene_db, "chat", user=member)
    assert result is not None and result.id == m_b.id
    # 授权模型非默认模型也不影响
    assert result.id != m_default.id


def test_model_scene_granted_default_in_list_preferred(scene_db):
    """granted 多模型授权: 列表内 is_default 的优先, 否则 id 最小。"""
    from src.web.models import AIModel, AIService

    svc = AIService(name="S", base_url="https://s", api_key="k")
    scene_db.add(svc)
    scene_db.flush()
    m1 = AIModel(name="M1", service_id=svc.id, model="m1", is_default=False)
    m2 = AIModel(name="M2", service_id=svc.id, model="m2", is_default=False)
    scene_db.add_all([m1, m2])
    scene_db.commit()

    member = _mk_user(
        scene_db, username="granted_multi",
        permissions={"model_access": {"mode": "granted", "model_ids": [m1.id, m2.id]}},
    )
    # 列表内无 default → id 升序第一个
    assert get_model_for_scene(scene_db, "chat", user=member).id == m1.id


def test_model_scene_granted_empty_list_denies(scene_db):
    """granted model_ids=[] = 显式全禁 → None。"""
    _mk_platform_model(scene_db)
    member = _mk_user(
        scene_db, username="granted_empty",
        permissions={"model_access": {"mode": "granted", "model_ids": []}},
    )
    assert get_model_for_scene(scene_db, "chat", user=member) is None


def test_model_scene_granted_prefers_scene_binding_in_list(scene_db):
    """granted 且场景绑定模型恰在列表内 → 场景绑定优先(平台编排保留)。"""
    from src.web.models import AIModel, AISceneBinding, AIService

    m_default = _mk_platform_model(scene_db, name="A", model="model-a", is_default=True)
    svc = AIService(name="S-B", base_url="https://sb", api_key="kb")
    scene_db.add(svc)
    scene_db.flush()
    m_b = AIModel(name="B", service_id=svc.id, model="model-b", is_default=False)
    scene_db.add(m_b)
    scene_db.flush()
    scene_db.add(AISceneBinding(scene="chat", model_id=m_b.id))
    scene_db.commit()

    member = _mk_user(
        scene_db, username="granted_bind",
        permissions={"model_access": {"mode": "granted", "model_ids": [m_default.id, m_b.id]}},
    )
    result = get_model_for_scene(scene_db, "chat", user=member)
    assert result.id == m_b.id


def test_model_scene_granted_denies_unlisted(scene_db):
    """mode=granted 且场景模型不在 model_ids → None。"""
    _mk_platform_model(scene_db)
    member = _mk_user(
        scene_db, username="granted_other",
        permissions={"model_access": {"mode": "granted", "model_ids": [99999]}},
    )
    assert get_model_for_scene(scene_db, "chat", user=member) is None


def test_model_scene_deny_all(scene_db):
    """mode=deny_all → None, 即使平台有模型。"""
    _mk_platform_model(scene_db)
    member = _mk_user(
        scene_db, username="denied_member",
        permissions={"model_access": {"mode": "deny_all", "model_ids": []}},
    )
    assert get_model_for_scene(scene_db, "chat", user=member) is None


def test_model_scene_byok_dict_priority(scene_db):
    """BYOK 优先于平台模型, 返回 dict 配置。"""
    _mk_platform_model(scene_db)
    member = _mk_user(scene_db, username="byok_member")
    _mk_byok(scene_db, member)
    result = get_model_for_scene(scene_db, "chat", user=member)
    assert isinstance(result, dict)
    assert result["base_url"] == "https://api.deepseek.com/v1"
    assert result["api_key"] == "sk-user"
    assert result["model"] == "deepseek-chat"
    assert result["is_default"] is True


def test_model_scene_byok_scene_match_wins(scene_db):
    """BYOK 内 scene 精确匹配优先于 is_default。"""
    member = _mk_user(scene_db, username="byok_scene")
    from src.web.models import UserAIService

    svc = UserAIService(
        user_id=member.id, name="双模型",
        base_url="https://x/v1", api_key="k",
        models_json=json.dumps([
            {"name": "chat默认", "model": "m-default", "is_default": True, "scene": ""},
            {"name": "chat专用", "model": "m-chat", "is_default": False, "scene": "chat"},
        ]),
    )
    scene_db.add(svc)
    scene_db.commit()
    result = get_model_for_scene(scene_db, "chat", user=member)
    assert result["model"] == "m-chat"


def test_model_scene_no_user_keeps_global(scene_db):
    """无 user(系统调用) → 原全局逻辑, 不受用户授权影响。"""
    m = _mk_platform_model(scene_db)
    assert get_model_for_scene(scene_db, "chat").id == m.id
    assert get_model_for_scene(scene_db, "chat", user=None).id == m.id


# ───────────────────────── 3. 中间件 RBAC ───────────────────────────


@pytest.fixture()
def client():
    from src.web.app import app

    return TestClient(app)


@pytest.fixture(autouse=True)
def clean_rbac_users():
    """清理本文件创建的用户(含 demo), 不干扰其他测试文件。"""
    yield
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        db.query(User).filter(
            User.username.in_(["demo", "rbac_member", "rbac_member_whitelist"])
        ).delete()
        db.commit()
    finally:
        db.close()


def _login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _create_user(client, token, username, role="member"):
    r = client.post(
        "/api/auth/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": username, "password": "rbac12345", "role": role},
    )
    assert r.status_code == 200, r.text


def _set_permissions(username, permissions):
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == username).first()
        u.permissions = permissions
        db.commit()
    finally:
        db.close()


def test_middleware_demo_readonly_and_admin_blocks(client, monkeypatch):
    """demo: 只读 + 自选增删例外 + 管理区403(settings/providers GET 可浏览)。"""
    monkeypatch.setattr("src.core.demo_limit.allow_api_get", lambda uid: True)
    admin_token = _login(client, "admin", "xz.170530")
    _create_user(client, admin_token, "demo", role="member")
    demo_token = _login(client, "demo", "rbac12345")
    H = {"Authorization": f"Bearer {demo_token}"}

    # 只读浏览放行
    assert client.get("/api/auth/me", headers=H).status_code == 200
    # 管理区 GET 403(datasources), 设置/服务商 GET 可浏览
    assert client.get("/api/datasources", headers=H).status_code == 403
    assert client.get("/api/settings", headers=H).status_code != 403
    assert client.get("/api/providers", headers=H).status_code != 403
    # 写操作 403(非自选)
    assert client.post("/api/settings", headers=H, json={}).status_code == 403
    # 自选增删例外(中间件放行, 接口层校验)
    r = client.post("/api/stocks", headers=H, json={"symbol": "000001", "market": "CN"})
    assert r.status_code != 403
    r = client.delete("/api/stocks/999999", headers=H)
    assert r.status_code != 403


def test_middleware_member_admin_403_readable_ok(client, monkeypatch):
    """member: 无 manage_* 时管理区403, 但 settings/providers GET 可浏览(向后兼容)。"""
    monkeypatch.setattr("src.core.demo_limit.allow_api_get", lambda uid: True)
    admin_token = _login(client, "admin", "xz.170530")
    _create_user(client, admin_token, "rbac_member", role="member")
    member_token = _login(client, "rbac_member", "rbac12345")
    H = {"Authorization": f"Bearer {member_token}"}

    assert client.get("/api/auth/me", headers=H).status_code == 200
    # 管理区 403
    assert client.get("/api/datasources", headers=H).status_code == 403
    assert client.post("/api/settings", headers=H, json={}).status_code == 403
    # 自查询例外(2026-08-16): member 可查自己的模块权限(导航过滤用), 不受 manage_users 限制
    r = client.get("/api/users/me/permissions", headers=H)
    assert r.status_code == 200, "me/permissions 应 200"
    data = r.json().get("data", {})
    assert "effective" in data and "manage_datasources" not in data.get("effective", []), "member 默认不应有 manage_datasources"
    # 可浏览例外(与 demo 一致)
    assert client.get("/api/settings", headers=H).status_code != 403
    assert client.get("/api/providers", headers=H).status_code != 403
    # 业务写操作不受影响(edit_watchlist/run_prediction)
    r = client.post("/api/stocks", headers=H, json={"symbol": "000001", "market": "CN"})
    assert r.status_code != 403
    r = client.post("/api/forecast/predict", headers=H, json={})
    assert r.status_code != 403


def test_middleware_owner_passes_all(client, monkeypatch):
    """owner: 管理区全过。"""
    monkeypatch.setattr("src.core.demo_limit.allow_api_get", lambda uid: True)
    admin_token = _login(client, "admin", "xz.170530")
    H = {"Authorization": f"Bearer {admin_token}"}

    assert client.get("/api/datasources", headers=H).status_code != 403
    assert client.post("/api/settings", headers=H, json={}).status_code != 403
    assert client.get("/api/users", headers=H).status_code != 403


def test_middleware_member_whitelist_grant(client, monkeypatch):
    """users.permissions 白名单: member 加 manage_datasources 后管理区放行。"""
    monkeypatch.setattr("src.core.demo_limit.allow_api_get", lambda uid: True)
    admin_token = _login(client, "admin", "xz.170530")
    _create_user(client, admin_token, "rbac_member_whitelist", role="member")
    _set_permissions("rbac_member_whitelist", ["manage_datasources"])
    member_token = _login(client, "rbac_member_whitelist", "rbac12345")
    H = {"Authorization": f"Bearer {member_token}"}

    assert client.get("/api/datasources", headers=H).status_code != 403
    # 未授权的管理区仍 403
    assert client.get("/api/users", headers=H).status_code == 403
    # /api/strategies 已移出管理区(2026-08-16: v0.2.47 策略库并入机会页,
    # 该前缀下全是只读/纯计算端点, member 机会页需要)
    assert client.get("/api/strategies/list", headers=H).status_code != 403
