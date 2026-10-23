"""2026-10-23 开发者向修复回归钉子（P0-1 / P0-2 / P1-3 / P1-4）。

每条都钉一个**真实故障**，不是“函数能跑”：

1. P0-1 调试台/文档把参数裸放请求体顶层 → 后端 `args` 恒 `{}`，
   所有需要参数（symbol 等）的 skill 在调试台必败且静默丢参；
   后端补了兼容垫片，**显式 args 仍优先**。
2. P0-2 `install.sh` 域名写死 `www.sida.hengsheng-elec.com`（多一级 www，
   且不随部署域名变化）→ 复制命令后验证必然失败。改为按请求推导。
3. P1-3 Pro/Owner 在 /api-keys 自助建 Key 被降级成 free（100 次/天）。
4. P1-4 两套删除接口语义相反：`/api/keys/my/{id}` 物理删、
   `/api/keys/{id}` 软删 → 用量审计行消失，与「已禁用 Key」统计自相矛盾。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN_USER = "admin"
ADMIN_PW = "admin-test-0606"
OLD_BASE_URL_HOST = "www.sida.hengsheng-elec.com"


# ── 夹具 ──────────────────────────────────────────────────────────────


@pytest.fixture()
def client(monkeypatch):
    """引导 owner（env 凭据）并返回 TestClient。

    与 tests/test_auth_bearer_priority.py 同款自洽化：先清干净**所有** owner，
    再按 env 显式重建，避免上游残留 owner 让 `get_or_create_owner` 短路。
    """
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    monkeypatch.setenv("AUTH_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AUTH_PASSWORD", ADMIN_PW)

    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        from tests.conftest import purge_users

        stale_ids = {str(u.id) for u in db.query(User).filter(User.role == "owner").all()}
        stale_ids |= {str(u.id) for u in db.query(User).filter(User.username == ADMIN_USER).all()}
        if stale_ids:
            purge_users(db, ids=sorted(stale_ids))

        from src.web.api.auth import get_or_create_owner

        get_or_create_owner(db)
    finally:
        db.close()

    # 建 Key 是 IP 级 5 次/小时滑动窗口（进程内），跨用例累计会误 429
    from src.web.api import skills_gateway as gw

    gw._key_reg_hits.clear()

    from src.web.app import app

    return TestClient(app)


def _login(client, username: str, password: str) -> str:  # noqa: ANN001
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _data(r):  # noqa: ANN001
    """统一响应信封：SIDA 的 JSON 接口包一层 {"data": ...}。"""
    body = r.json()
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _mk_member(client, token: str, username: str = "devfix_member") -> dict:  # noqa: ANN001
    r = client.post(
        "/api/auth/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": username, "password": "devfix12345678", "role": "member"},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["user"]


# ── P0-1 请求体契约 + 兼容垫片 ─────────────────────────────────────────


def test_skill_run_request_folds_bare_top_level_args():
    """裸参（旧文档/旧调试台写法）经垫片后必须能被 handler 读到。"""
    from src.web.api.skills_gateway import SkillRunRequest

    req = SkillRunRequest.model_validate({"symbol": "000001", "format": "json"})
    assert req.args == {"symbol": "000001"}, f"裸参被丢弃: {req.args!r}"
    assert req.format == "json"


def test_skill_run_request_explicit_args_win_over_top_level():
    """显式 args 优先于顶层同名键（垫片只兜底，不改语义）。"""
    from src.web.api.skills_gateway import SkillRunRequest

    req = SkillRunRequest.model_validate({"args": {"symbol": "000001"}, "symbol": "600000"})
    assert req.args == {"symbol": "000001"}

    # 正式契约不受影响
    req2 = SkillRunRequest.model_validate({"args": {"symbol": "600000"}, "format": "json"})
    assert req2.args == {"symbol": "600000"} and req2.format == "json"


def test_run_endpoint_receives_args_from_bare_body(client, monkeypatch):  # noqa: ANN001
    """端到端：裸参请求体 → 端点 handler 确实收到 symbol（旧代码收到 {}）。"""
    from src.web.api import skills_gateway as gw

    captured: list[dict] = []

    class _StubTool:
        caliber = "test"
        schema = {"function": {"name": "get_stock_quote", "description": "stub", "parameters": {}}}

        async def handler(self, db, args, user):  # noqa: ANN001, ANN202
            captured.append(dict(args))
            return '{"stub": true}'

    monkeypatch.setitem(gw.CHAT_TOOL_REGISTRY, "get_stock_quote", _StubTool())

    token = _login(client, ADMIN_USER, ADMIN_PW)
    r = client.post(
        "/api/skills/get_stock_quote/run",
        headers={"Authorization": f"Bearer {token}"},
        json={"symbol": "000001", "format": "json"},  # 旧写法：参数裸放顶层
    )
    assert r.status_code == 200, r.text
    assert captured, "handler 未被调用"
    assert captured[0].get("symbol") == "000001", f"handler 收到 {captured[0]!r}"

    # 正式契约同样可用
    r2 = client.post(
        "/api/skills/get_stock_quote/run",
        headers={"Authorization": f"Bearer {token}"},
        json={"args": {"symbol": "600000"}, "format": "json"},
    )
    assert r2.status_code == 200, r2.text
    assert captured[1].get("symbol") == "600000"


# ── P0-2 install.sh 域名按请求推导 ─────────────────────────────────────


def test_install_sh_uses_forwarded_host(client):  # noqa: ANN001
    r = client.get(
        "/api/skills/install.sh",
        headers={"X-Forwarded-Host": "sida.example.com", "X-Forwarded-Proto": "https"},
    )
    assert r.status_code == 200, r.text
    body = r.text
    assert "https://sida.example.com" in body
    assert OLD_BASE_URL_HOST not in body, "install.sh 仍写死旧域名"
    # 注入值必须落在默认 BASE_URL 上，否则复制后仍打旧域名
    assert 'BASE_URL="${SIDA_BASE_URL:-https://sida.example.com}"' in body


def test_install_sh_falls_back_to_host_header(client):  # noqa: ANN001
    r = client.get("/api/skills/install.sh", headers={"Host": "10.0.0.5:8080"})
    assert r.status_code == 200, r.text
    assert "http://10.0.0.5:8080" in r.text
    assert OLD_BASE_URL_HOST not in r.text


# ── P1-3 Pro/Owner 建 Key 拿 pro 档 ───────────────────────────────────


def test_owner_create_key_gets_pro_tier(client):  # noqa: ANN001
    token = _login(client, ADMIN_USER, ADMIN_PW)
    r = client.post("/api/keys/my", headers={"Authorization": f"Bearer {token}"}, json={})
    assert r.status_code == 200, r.text
    key = _data(r)["key"]
    assert key["tier"] == "pro", f"owner 建 Key 拿到 {key['tier']} 档"
    assert key["expires_at"] is None


def test_member_create_key_stays_free(client):  # noqa: ANN001
    owner_token = _login(client, ADMIN_USER, ADMIN_PW)
    _mk_member(client, owner_token, username="devfix_member_free")
    member_token = _login(client, "devfix_member_free", "devfix12345678")

    r = client.post("/api/keys/my", headers={"Authorization": f"Bearer {member_token}"}, json={})
    assert r.status_code == 200, r.text
    key = _data(r)["key"]
    assert key["tier"] == "free", f"member 建 Key 拿到 {key['tier']} 档"


# ── P1-4 两套删除接口统一软删 ─────────────────────────────────────────


def test_delete_my_key_is_soft_delete(client):  # noqa: ANN001
    token = _login(client, ADMIN_USER, ADMIN_PW)
    r = client.post("/api/keys/my", headers={"Authorization": f"Bearer {token}"}, json={})
    assert r.status_code == 200, r.text
    key_id = _data(r)["key"]["id"]

    d = client.delete(f"/api/keys/my/{key_id}", headers={"Authorization": f"Bearer {token}"})
    assert d.status_code == 200, d.text

    from src.web.database import SessionLocal
    from src.web.models import SkillApiKey

    db = SessionLocal()
    try:
        row = db.query(SkillApiKey).filter(SkillApiKey.id == key_id).first()
        assert row is not None, "删除后行被物理删除 → 用量审计断链"
        assert row.status == "disabled"
        assert row.frozen_reason
    finally:
        db.close()
