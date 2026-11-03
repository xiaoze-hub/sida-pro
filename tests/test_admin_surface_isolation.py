"""管理面隔离（2026-11-03）：管理接口只认 admin 受众 token。

背景：全站原本一个 JWT secret，用户 token 与 owner token 只差 DB 里一个字段；
仓库里 `require_owner` 有 86 处手写调用点（12 个模块），漏一处就是提权口。

改动后：
- 用户面 token 用 `JWT_SECRET` 签、`aud=user`；
- 管理面 token 用 `get_admin_jwt_secret()` 签、`aud=admin`
  （cookie `sida_admin_token` 或头 `X-Admin-Token`，默认 4h）；
- `require_owner` 语义升级为 `require_admin`，86 处调用点自动收紧。

断言用真实 HTTP，跑在 `/api/users/{uid}/permissions`（`require_owner` 的 GET 端点）：
① 无凭证 401；② owner 的**用户面** token 401；③ 用户 token + admin token 才 200；
④ member 拿到 admin token 也 403（不给非 owner 提权）。

注意：不要拿"管理区前缀 GET 一律 401"当判据 —— `/api/providers`、`/api/agents`
这类前缀下有一批**只读**端点本来就对登录用户开放（RBAC 只管写），会假红。
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from src.core.auth_tokens import create_admin_token, create_token
from src.db.models import User
from src.web.database import SessionLocal


@pytest.fixture(scope="module", autouse=True)
def _strict_admin_mode():
    """本文件必须在**严格模式**下跑（= 生产默认值），否则测不到隔离本身。"""
    old = os.environ.get("STRICT_ADMIN_TOKEN")
    os.environ["STRICT_ADMIN_TOKEN"] = "1"
    yield
    if old is None:
        os.environ.pop("STRICT_ADMIN_TOKEN", None)
    else:
        os.environ["STRICT_ADMIN_TOKEN"] = old


@pytest.fixture(scope="module")
def ctx():
    from src.web.app import app

    suffix = uuid.uuid4().hex[:8]
    owner_id, member_id = str(uuid.uuid4()), str(uuid.uuid4())

    db = SessionLocal()
    try:
        owner = User(
            id=owner_id, username=f"own_{suffix}", password_hash="x",
            role="owner", is_active=True, token_version=1,
        )
        member = User(
            id=member_id, username=f"mem_{suffix}", password_hash="x",
            role="member", is_active=True, token_version=1,
        )
        db.add_all([owner, member])
        db.commit()
        tok = {
            "owner_user": create_token(owner).token,
            "owner_admin": create_admin_token(owner).token,
            "member_user": create_token(member).token,
            "member_admin": create_admin_token(member).token,
        }
    finally:
        db.close()

    with TestClient(app) as client:
        yield {"client": client, "tok": tok, "owner_id": owner_id}

    db = SessionLocal()
    try:
        db.query(User).filter(User.id.in_([owner_id, member_id])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _url(ctx) -> str:
    return f"/api/users/{ctx['owner_id']}/permissions"


def _get(ctx, headers=None):
    return ctx["client"].get(_url(ctx), headers=headers or {})


def test_无凭证401(ctx):
    assert _get(ctx).status_code == 401


def test_owner的用户面token被拒401(ctx):
    """核心：DB 里 role=owner 也不够 —— 必须持有 admin 受众 token。"""
    r = _get(ctx, {"Authorization": f"Bearer {ctx['tok']['owner_user']}"})
    assert r.status_code == 401, f"用户面 token 能触达管理端点: {r.status_code} {r.text[:120]}"


def test_用户token加admin_token才放行(ctx):
    """生产路径：浏览器同时带用户 cookie 与 admin cookie。"""
    r = _get(ctx, {
        "Authorization": f"Bearer {ctx['tok']['owner_user']}",
        "X-Admin-Token": ctx["tok"]["owner_admin"],
    })
    assert r.status_code == 200, f"合法组合被拒: {r.status_code} {r.text[:160]}"


def test_member即使拿到admin_token也403(ctx):
    """admin token 不是万能钥匙：非 owner 仍然进不来。"""
    r = _get(ctx, {
        "Authorization": f"Bearer {ctx['tok']['member_user']}",
        "X-Admin-Token": ctx["tok"]["member_admin"],
    })
    assert r.status_code == 403, f"member 提权成功: {r.status_code} {r.text[:120]}"


def test_require_admin依赖三种凭证(ctx):
    """依赖层三态（不依赖路由/中间件）。"""
    import asyncio

    from fastapi import HTTPException
    from starlette.requests import Request

    from src.web.api.auth import require_admin

    def _req(headers=None):
        return Request({
            "type": "http", "method": "GET", "path": "/api/users",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "query_string": b"", "client": ("127.0.0.1", 1),
        })

    db = SessionLocal()
    try:
        with pytest.raises(HTTPException) as e1:
            asyncio.run(require_admin(_req(), db))
        assert e1.value.status_code == 401

        with pytest.raises(HTTPException) as e2:
            asyncio.run(require_admin(_req({"X-Admin-Token": ctx["tok"]["owner_user"]}), db))
        assert e2.value.status_code == 401

        user = asyncio.run(require_admin(_req({"X-Admin-Token": ctx["tok"]["owner_admin"]}), db))
        assert str(user.id) == ctx["owner_id"] and user.role == "owner"
    finally:
        db.close()
