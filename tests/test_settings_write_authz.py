# -*- coding: utf-8 -*-
"""平台设置写接口的权限收口(2026-11-07 独立复审 P0)。

漏洞(实测): `PUT /api/settings/{key}` 只在 `/api/settings` **前缀**上由
`demo_isolation_middleware` 拦 `manage_settings`; 而 `ApiVersionAliasMiddleware`
是**内层**(后 add 的 demo 中间件在外层), 它把 `/api/v1/settings/xxx` 改写成
`/api/settings/xxx`。于是:

    PUT /api/settings/http_proxy      → 403(member 被中间件拦)
    PUT /api/v1/settings/http_proxy   → 200(member 写成功)

member 因此可改 wudao/zhitu/tdx token、http_proxy(把全站出网流量导向自己的代理)、
头像等全局配置。端点自身**没有任何权限依赖**, 唯一防线是一个路径前缀判断 ——
任何前缀变形(版本别名 / 未来新增别名)都会漏。

本文件的验收线(全部真实 HTTP, 真 app + 真 DB, 不打桩权限):
1. member 写 /api/settings/{key} 与 /api/v1/settings/{key} 均 403, DB 不变
2. 敏感键与普通键(含 http_proxy / 头像)同一个门槛, member 一律 403
3. 无身份 401; 服务身份(X-Service-Token / role=service)一律拒绝
4. owner 可写; 响应/审计都不回明文凭证; 审计行落到 audit_logs
5. **提权撤销**: DB 降权为 member 后, 旧 owner token(claim 仍是 owner)立即失效
   —— 端点必须实时取 DB 角色, 不能信 JWT claim
6. **审计失败不得谎报成功**: 审计写不进去 → 不写设置、5xx
7. 读兼容: member 仍可 GET /api/settings(敏感键已掩码)、GET /avatar、GET /version
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN_USER = "authz_admin"
ADMIN_PW = "authz_admin_pw_1234"
MEMBER_USER = "authz_member"
MEMBER_PW = "authz_member_pw_1234"
OWNER2_USER = "authz_owner2"
OWNER2_PW = "authz_owner2_pw_1234"
SECRET_KEY = "zhitu_token"       # 数据源凭证
PLAIN_KEY = "http_proxy"         # 全局出网代理(非凭证, 但同样是全站配置)
FAKE_SECRET = "zhitu-fake-0001"
NEW_SECRET = "zhitu-fake-0002"


# ── fixtures ────────────────────────────────────────────────────────────
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    monkeypatch.setenv("AUTH_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AUTH_PASSWORD", ADMIN_PW)
    from src.web.database import SessionLocal
    from tests.conftest import purge_users

    db = SessionLocal()
    try:
        purge_users(db)
    finally:
        db.close()
    from src.web.app import app

    c = TestClient(app)
    try:
        yield c
    finally:
        c.cookies.clear()


def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


@pytest.fixture()
def owner_token(client) -> str:
    return _login(client, ADMIN_USER, ADMIN_PW)


@pytest.fixture()
def member_token(client, owner_token) -> str:
    r = client.post(
        "/api/auth/users",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"username": MEMBER_USER, "password": MEMBER_PW, "role": "member"},
    )
    assert r.status_code == 200, r.text
    return _login(client, MEMBER_USER, MEMBER_PW)


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _db_value(key: str) -> str | None:
    from src.web.database import SessionLocal
    from src.web.models import AppSettings

    db = SessionLocal()
    try:
        row = db.query(AppSettings).filter(AppSettings.key == key).first()
        return None if row is None else row.value
    finally:
        db.close()


def _set_db(key: str, value: str) -> None:
    from src.web.database import SessionLocal
    from src.web.models import AppSettings

    db = SessionLocal()
    try:
        row = db.query(AppSettings).filter(AppSettings.key == key).first()
        if row:
            row.value = value
        else:
            db.add(AppSettings(key=key, value=value, description=""))
        db.commit()
    finally:
        db.close()


def _set_role(username: str, role: str) -> None:
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == username).first()
        assert u is not None
        u.role = role
        db.commit()
    finally:
        db.close()


def _audit_rows(action_prefix: str) -> list:
    from src.web.database import SessionLocal
    from src.web.models import AuditLog

    db = SessionLocal()
    try:
        return (
            db.query(AuditLog)
            .filter(AuditLog.action.like(action_prefix + "%"))
            .order_by(AuditLog.id.desc())
            .all()
        )
    finally:
        db.close()


_TOUCHED_KEYS = (SECRET_KEY, PLAIN_KEY, "ui_avatar")


def _snapshot_keys(keys) -> dict:
    from src.web.database import SessionLocal
    from src.web.models import AppSettings

    db = SessionLocal()
    try:
        return {
            k: (lambda r: (r is not None, r.value if r is not None else None))(
                db.query(AppSettings).filter(AppSettings.key == k).first()
            )
            for k in keys
        }
    finally:
        db.close()


def _restore_keys(snap: dict) -> None:
    from src.web.database import SessionLocal
    from src.web.models import AppSettings

    db = SessionLocal()
    try:
        for k, (existed, value) in snap.items():
            row = db.query(AppSettings).filter(AppSettings.key == k).first()
            if existed and row is not None:
                row.value = value
            elif not existed and row is not None:
                db.delete(row)
            elif existed and row is None:
                db.add(AppSettings(key=k, value=value, description=""))
        db.commit()
    finally:
        db.close()


@pytest.fixture(autouse=True)
def _reset_settings():
    """每个用例前把关键设置置成已知值; 用完**还原**, 不污染同库的其它测试文件。

    (共享库: 别的用例会断言"库里有/没有 zhitu token", 本文件写过 fake 值必须还回去。)
    """
    snap = _snapshot_keys(_TOUCHED_KEYS)
    _set_db(SECRET_KEY, FAKE_SECRET)
    _set_db(PLAIN_KEY, "")
    try:
        yield
    finally:
        _restore_keys(snap)
        _purge_audit_rows()


def _purge_audit_rows() -> None:
    from src.web.database import SessionLocal
    from src.web.models import AuditLog

    db = SessionLocal()
    try:
        db.query(AuditLog).filter(AuditLog.action.like("settings_update:%")).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


# ── 1/2. member 写: 任何路径形态都必须 403 且不落库 ────────────────────
@pytest.mark.parametrize(
    "path",
    [
        "/api/settings/" + SECRET_KEY,
        "/api/v1/settings/" + SECRET_KEY,   # 版本别名曾绕过前缀中间件
        "/api/settings/" + PLAIN_KEY,
        "/api/v1/settings/" + PLAIN_KEY,
    ],
)
def test_member_cannot_write_global_settings(client, member_token, path):
    before = _db_value(SECRET_KEY if SECRET_KEY in path else PLAIN_KEY)
    r = client.put(path, headers=_hdr(member_token), json={"value": "pwned-by-member"})
    assert r.status_code == 403, f"{path} 未拦截: {r.status_code} {r.text[:200]}"
    after = _db_value(SECRET_KEY if SECRET_KEY in path else PLAIN_KEY)
    assert after == before, f"{path} 写穿了: {before!r} → {after!r}"


@pytest.mark.parametrize("path", ["/api/settings/avatar", "/api/v1/settings/avatar"])
def test_member_cannot_write_global_avatar(client, member_token, path):
    """ui_avatar 是全局单行(不是 per-user), 属全局设置写, 与其它键同门槛。"""
    r = client.put(
        path,
        headers=_hdr(member_token),
        json={"value": "data:image/jpeg;base64,AAAA"},
    )
    assert r.status_code == 403, f"{path} 未拦截: {r.status_code} {r.text[:200]}"


def test_member_cannot_clear_secret(client, member_token):
    r = client.put(
        "/api/v1/settings/" + SECRET_KEY,
        headers=_hdr(member_token),
        json={"value": "", "clear": True},
    )
    assert r.status_code == 403, r.text
    assert _db_value(SECRET_KEY) == FAKE_SECRET


# ── 3. 无身份 / 服务身份 ───────────────────────────────────────────────
def test_anonymous_cannot_write(client):
    client.cookies.clear()  # 去掉登录残留 cookie, 真正无身份
    for path in ("/api/settings/" + PLAIN_KEY, "/api/v1/settings/" + PLAIN_KEY):
        r = client.put(path, json={"value": "anon"})
        assert r.status_code in (401, 403), f"{path}: {r.status_code} {r.text[:200]}"
        assert r.status_code != 200
    assert _db_value(PLAIN_KEY) == ""


def test_service_token_only_cannot_write(client):
    client.cookies.clear()
    from src.web.api.auth import get_service_token

    r = client.put(
        "/api/v1/settings/" + PLAIN_KEY,
        headers={"X-Service-Token": get_service_token()},
        json={"value": "svc"},
    )
    assert r.status_code in (401, 403), r.text
    assert _db_value(PLAIN_KEY) == ""


def test_service_principal_jwt_cannot_write(client, member_token):
    """身份角色为 service 的账号(服务身份)不得写全局设置。"""
    client.cookies.clear()
    _set_role(MEMBER_USER, "service")
    from datetime import datetime, timedelta, timezone

    import jwt

    from src.core.auth_tokens import JWT_ALGORITHM, get_jwt_secret
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == MEMBER_USER).first()
        assert u is not None
        token = jwt.encode(
            {
                "sub": u.id,
                "username": u.username,
                "role": "service",
                "ver": u.token_version,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            get_jwt_secret(),
            algorithm=JWT_ALGORITHM,
        )
    finally:
        db.close()

    r = client.put(
        "/api/v1/settings/" + PLAIN_KEY,
        headers=_hdr(token),
        json={"value": "svc-jwt"},
    )
    assert r.status_code == 403, f"{r.status_code} {r.text[:200]}"
    assert _db_value(PLAIN_KEY) == ""


# ── 4/5. owner 正常写 + 提权撤销 ───────────────────────────────────────
def test_owner_can_write_and_audit_has_no_plaintext(client, owner_token):
    r = client.put(
        "/api/v1/settings/" + SECRET_KEY,
        headers=_hdr(owner_token),
        json={"value": "  " + NEW_SECRET + "  "},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    payload = body.get("data", body)
    assert NEW_SECRET not in str(body), f"响应回吐明文: {body}"
    assert _db_value(SECRET_KEY) == NEW_SECRET  # 两侧空白被 strip

    rows = _audit_rows(f"settings_update:{SECRET_KEY}")
    assert rows, "owner 写入没有落审计"
    detail = rows[0].detail
    assert rows[0].username == ADMIN_USER
    assert NEW_SECRET not in detail, f"审计泄露明文凭证: {detail!r}"
    assert "********" in detail, f"敏感值应掩码: {detail!r}"


def test_owner_can_write_plain_key(client, owner_token):
    r = client.put(
        "/api/settings/" + PLAIN_KEY,
        headers=_hdr(owner_token),
        json={"value": "http://127.0.0.1:7890"},
    )
    assert r.status_code == 200, r.text
    assert _db_value(PLAIN_KEY) == "http://127.0.0.1:7890"


@pytest.fixture()
def owner2_token(client, owner_token) -> str:
    """第二个 owner: 用于"降权立即生效"用例。

    不能把唯一 owner 降权 —— 那会触发 `get_or_create_owner` 重新插入同名 owner,
    报 UNIQUE 而不是我们想验的 403。
    """
    r = client.post(
        "/api/auth/users",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"username": OWNER2_USER, "password": OWNER2_PW, "role": "owner"},
    )
    assert r.status_code == 200, r.text
    return _login(client, OWNER2_USER, OWNER2_PW)


def test_demotion_takes_effect_immediately_even_on_alias_path(client, owner2_token):
    """DB 降权后旧 owner token(claim 仍写 owner)必须立刻失效 —— 实时取 DB 角色。

    走 /api/v1 是为了绕开外层前缀中间件, 单验端点自身的判权(不靠中间件兜底)。
    """
    assert client.put(
        "/api/settings/" + PLAIN_KEY, headers=_hdr(owner2_token), json={"value": "ok-before"}
    ).status_code == 200
    _set_role(OWNER2_USER, "member")
    r = client.put(
        "/api/v1/settings/" + PLAIN_KEY,
        headers=_hdr(owner2_token),
        json={"value": "ok-after-demotion"},
    )
    assert r.status_code == 403, f"降权未生效: {r.status_code} {r.text[:200]}"
    assert _db_value(PLAIN_KEY) == "ok-before"


def test_allowlist_grant_still_works(client, owner_token, member_token):
    """既有兼容通道: owner 在 users.permissions 里单独给 member 开 manage_settings。

    端点判权必须与中间件同口径(角色权限 ∪ 白名单), 否则这条合法授权会变成幽灵 403。
    """
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == MEMBER_USER).first()
        u.permissions = ["manage_settings"]
        db.commit()
    finally:
        db.close()

    r = client.put(
        "/api/v1/settings/" + PLAIN_KEY,
        headers=_hdr(member_token),
        json={"value": "http://granted-proxy:8080"},
    )
    assert r.status_code == 200, f"白名单授权被误杀: {r.status_code} {r.text[:200]}"
    assert _db_value(PLAIN_KEY) == "http://granted-proxy:8080"


# ── 6. 审计失败不得谎报成功 ────────────────────────────────────────────
def test_audit_stage_failure_never_returns_success(client, owner_token, monkeypatch):
    """审计行连"挂进事务"都失败 → 绝不返回 200, 设置也不落库。"""
    from fastapi.testclient import TestClient

    import src.web.app as app_module
    from src.web.api import settings as settings_api

    def _boom(*a, **k):
        raise RuntimeError("audit stage failed")

    monkeypatch.setattr(settings_api, "_stage_audit", _boom, raising=True)
    c = TestClient(app_module.app, raise_server_exceptions=False)
    r = c.put(
        "/api/settings/" + PLAIN_KEY,
        headers=_hdr(owner_token),
        json={"value": "should-not-land"},
    )
    assert r.status_code != 200, f"审计失败仍返回成功: {r.status_code} {r.text[:200]}"
    assert r.status_code >= 400
    assert _db_value(PLAIN_KEY) != "should-not-land", "审计失败却写进了设置"


def test_audit_commit_failure_rolls_back(client, owner_token, monkeypatch):
    """审计行在 flush 时失败(表锁/约束) → 整笔回滚 + 5xx, 设置保持原值。"""
    from sqlalchemy.orm import Session as _S

    import src.db.session as db_session
    from src.web.models import AuditLog

    real_commit = _S.commit

    def _patched(self, *a, **k):
        if any(isinstance(obj, AuditLog) for obj in self.new):
            raise RuntimeError("audit_logs table locked")
        return real_commit(self, *a, **k)

    monkeypatch.setattr(db_session.RoutingSession, "commit", _patched, raising=True)
    before_rows = len(_audit_rows(f"settings_update:{PLAIN_KEY}"))
    r = client.put(
        "/api/settings/" + PLAIN_KEY,
        headers=_hdr(owner_token),
        json={"value": "should-not-land-either"},
    )
    assert r.status_code == 503, f"预期 503(fail closed), 实际 {r.status_code} {r.text[:200]}"
    assert _db_value(PLAIN_KEY) != "should-not-land-either", "审计失败却写进了设置"
    assert len(_audit_rows(f"settings_update:{PLAIN_KEY}")) == before_rows, (
        "审计失败却留下了审计行/未回滚"
    )


# ── 7. 读兼容: 普通成员仍能安全读取 ────────────────────────────────────
def test_member_reads_still_work_and_secrets_masked(client, member_token):
    r = client.get("/api/settings", headers=_hdr(member_token))
    assert r.status_code == 200, r.text
    data = r.json()
    items = data.get("data", data)
    by_key = {i["key"]: i for i in items}
    assert SECRET_KEY in by_key
    assert by_key[SECRET_KEY]["value"] in ("", "********"), by_key[SECRET_KEY]["value"]
    assert FAKE_SECRET not in str(r.text), "列表接口泄露明文凭证"

    for path in ("/api/settings/avatar", "/api/settings/version"):
        resp = client.get(path, headers=_hdr(member_token))
        assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text[:120]}"


# ── 8. 端点级判权独立成立(不靠中间件) ─────────────────────────────────
def test_endpoint_gate_holds_without_middleware(client, member_token, owner_token):
    """把 settings.router 单独挂到一个**没有** demo 隔离中间件的 app 上。

    这是本次修复的核心不变量: 判权写在端点依赖里, 所以即便外层前缀中间件不存在
    (被绕过 / 被改写 / 以后重构), member 也写不进全局设置。
    """
    from datetime import datetime, timedelta, timezone

    import jwt
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.core.auth_tokens import JWT_ALGORITHM, get_jwt_secret
    from src.web.api import settings as settings_api
    from src.web.database import SessionLocal, get_db
    from src.web.models import User

    app = FastAPI()
    app.include_router(settings_api.router, prefix="/settings")

    def _db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    c = TestClient(app)

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == MEMBER_USER).first()
        assert u is not None
        token = jwt.encode(
            {
                "sub": u.id,
                "username": u.username,
                "role": "member",          # 故意给 member claim, 且 DB 角色也是 member
                "ver": u.token_version,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            get_jwt_secret(),
            algorithm=JWT_ALGORITHM,
        )
    finally:
        db.close()

    before = _db_value(SECRET_KEY)
    r = c.put("/settings/" + SECRET_KEY, headers=_hdr(token), json={"value": "no-way"})
    assert r.status_code == 403, f"端点未独立判权: {r.status_code} {r.text[:200]}"
    assert _db_value(SECRET_KEY) == before

    # 同一 app 下 owner 仍然能写(证明确实是判权, 而不是把路由写坏了)
    ok = c.put("/settings/" + PLAIN_KEY, headers=_hdr(owner_token), json={"value": "ok"})
    assert ok.status_code == 200, ok.text


def test_endpoint_gate_fails_closed_on_db_error(client, owner_token, monkeypatch):
    """角色查询炸了也必须拒绝(不能"查不到就放行")。

    故意用 **owner** token: 若实现是 fail-open, 这里就会 200 写穿;
    只有真正 fail closed 才会 403。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import settings as settings_api
    from src.web.database import SessionLocal, get_db

    app = FastAPI()
    app.include_router(settings_api.router, prefix="/settings")

    def _db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db

    class _BrokenSession:
        def query(self, *a, **k):
            raise RuntimeError("db down")

        def close(self):
            pass

    # 必须补丁 settings 模块里**已绑定**的那个名字(模块级 from ... import)
    monkeypatch.setattr(settings_api, "WriteSessionLocal", lambda: _BrokenSession())
    before = _db_value(PLAIN_KEY)
    r = TestClient(app).put(
        "/settings/" + PLAIN_KEY, headers=_hdr(owner_token), json={"value": "db-down-write"}
    )
    assert r.status_code == 403, f"判权异常未 fail closed: {r.status_code} {r.text[:200]}"
    assert _db_value(PLAIN_KEY) == before
