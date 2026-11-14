# -*- coding: utf-8 -*-
"""机会页「全局写」入口的角色范围(2026-11-12 复审二)。

审计问题:
    `POST /api/recommendations/strategy-signals/refresh`、
    `POST .../strategy-signals/outcomes/evaluate`、
    `POST .../entry-candidates/refresh`、
    `POST .../entry-candidates/outcomes/evaluate`、
    以及 `GET .../entry-candidates?refresh=true`
    都会写**全站共用**的候选/信号/后验数据, 但只挂了 `protected`(登录即可):
    guest 试用号、服务 token 都能反复触发全市场重算。

本文件验收线(真实 HTTP + 真 app + 隔离临时库, 不打桩判权):
1. 无 token → 401/403; guest 与服务身份 → 403, 且**一个核心写函数都没被调用**;
2. member / pro / owner 仍可用 —— 机会页「刷新」是既有成员功能, 不能被误杀;
3. 只读入口(列表/状态)保持登录即可, 权限收口只加在写上;
4. DB 降权后旧 token 立即失去写权限(实时读库角色, 不信 JWT claim)。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN_USER = "rec_scope_admin"
ADMIN_PW = "rec_scope_admin_pw_1234"
MEMBER_USER = "rec_scope_member"
MEMBER_PW = "rec_scope_member_pw_1234"
PRO_USER = "rec_scope_pro"
PRO_PW = "rec_scope_pro_pw_1234"
GUEST_USER = "rec_scope_guest"
GUEST_PW = "rec_scope_guest_pw_1234"
ALL_USERS = (ADMIN_USER, MEMBER_USER, PRO_USER, GUEST_USER)

DB_ENV_KEYS = (
    "DATABASE_URL",
    "DATABASE_URL_WRITE",
    "DATABASE_URL_READ",
    "DATABASE_URL_MIGRATE",
    "SIDA_DB_URL",
    "SIDA_DB",
    "SIDA_APP_DB_ROLE",
)

#: 全部「会写全站数据」的 HTTP 入口。
WRITE_ENTRY_POINTS = (
    ("POST", "/api/recommendations/strategy-signals/refresh"),
    ("POST", "/api/recommendations/strategy-signals/outcomes/evaluate"),
    ("POST", "/api/recommendations/entry-candidates/refresh"),
    ("POST", "/api/recommendations/entry-candidates/outcomes/evaluate"),
    ("GET", "/api/recommendations/entry-candidates?refresh=true"),
)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    for key in DB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    from src.db.session import write_engine

    url = str(write_engine.url)
    assert url.startswith("sqlite"), f"测试库必须是隔离 SQLite, 实际: {url}"
    assert "/data/sida.db" not in url


@pytest.fixture()
def calls(monkeypatch):
    """把四个核心写函数换成计数器: 断言「被拒绝的请求没有开始写」。"""
    from src.web.api import recommendations as rec

    counters = {"refresh_signals": 0, "refresh_candidates": 0,
                "eval_signals": 0, "eval_candidates": 0}

    monkeypatch.setattr(
        rec, "refresh_strategy_signals",
        lambda **kw: counters.__setitem__("refresh_signals", counters["refresh_signals"] + 1) or {
            "snapshot_date": "2026-11-12", "count": 0, "items": []},
    )
    monkeypatch.setattr(
        rec, "refresh_entry_candidates",
        lambda **kw: counters.__setitem__("refresh_candidates", counters["refresh_candidates"] + 1) or {
            "snapshot_date": "2026-11-12"},
    )
    monkeypatch.setattr(
        rec, "evaluate_strategy_outcomes",
        lambda **kw: counters.__setitem__("eval_signals", counters["eval_signals"] + 1) or {"evaluated": 0},
    )
    monkeypatch.setattr(
        rec, "evaluate_entry_candidate_outcomes",
        lambda **kw: counters.__setitem__("eval_candidates", counters["eval_candidates"] + 1) or {"evaluated": 0},
    )
    monkeypatch.setattr(rec, "list_entry_candidates", lambda **kw: {"items": [], "count": 0})
    return counters


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    monkeypatch.setenv("AUTH_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AUTH_PASSWORD", ADMIN_PW)
    from src.web.database import SessionLocal
    from tests.conftest import purge_users

    db = SessionLocal()
    try:
        for name in ALL_USERS:
            purge_users(db, only_username=name)
        _ensure_owner(db, ADMIN_USER, ADMIN_PW)
        db.commit()
    finally:
        db.close()

    from src.web.app import app

    c = TestClient(app)
    try:
        yield c
    finally:
        c.cookies.clear()
        db = SessionLocal()
        try:
            for name in ALL_USERS:
                purge_users(db, only_username=name)
        finally:
            db.close()


def _ensure_owner(db, username: str, password: str) -> None:
    from src.web.api.auth import create_user, hash_password
    from src.web.models import User

    row = db.query(User).filter(User.username == username).first()
    if row is None:
        create_user(db, username, password, "owner")
        return
    row.password_hash = hash_password(password)
    row.role = "owner"
    row.is_active = True


def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _hdr(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _make_user(c: TestClient, owner_token: str, username: str, password: str, role: str = "member") -> str:
    r = c.post(
        "/api/auth/users",
        headers=_hdr(owner_token),
        json={"username": username, "password": password, "role": role},
    )
    assert r.status_code == 200, r.text
    return _login(c, username, password)


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


def _call(c: TestClient, method: str, path: str, token: str | None, **kwargs):
    return c.request(method, path, headers=_hdr(token), **kwargs)


@pytest.fixture()
def owner_token(client) -> str:
    return _login(client, ADMIN_USER, ADMIN_PW)


@pytest.fixture()
def member_token(client, owner_token) -> str:
    return _make_user(client, owner_token, MEMBER_USER, MEMBER_PW, "member")


@pytest.fixture()
def pro_token(client, owner_token) -> str:
    token = _make_user(client, owner_token, PRO_USER, PRO_PW, "member")
    _set_role(PRO_USER, "pro")
    return token


@pytest.fixture()
def guest_token(client, owner_token) -> str:
    return _make_user(client, owner_token, GUEST_USER, GUEST_PW, "guest")


# ── 1. 无身份 / 试用档 / 服务身份: 拒绝且零副作用 ──────────────────────
@pytest.mark.parametrize("method,path", WRITE_ENTRY_POINTS)
def test_no_token_is_rejected(client, calls, method, path):
    r = _call(client, method, path, None)
    assert r.status_code in (401, 403), r.text
    assert sum(calls.values()) == 0, f"被拒绝的请求仍触发了写: {calls}"


@pytest.mark.parametrize("method,path", WRITE_ENTRY_POINTS)
def test_guest_cannot_trigger_global_writes(client, guest_token, calls, method, path):
    """guest(试用号)是全站只读档: 不得触发全市场重算/后验。"""
    r = _call(client, method, path, guest_token)
    assert r.status_code == 403, f"guest 竟能触发全局写: {r.status_code} {r.text[:200]}"
    assert sum(calls.values()) == 0, f"guest 被拒后仍写了数据: {calls}"


@pytest.mark.parametrize("method,path", WRITE_ENTRY_POINTS)
def test_service_identity_rejected(client, calls, method, path):
    r = client.request(
        method, path,
        headers={"X-Service-Token": "svc-token", "X-Service-Role": "service"},
    )
    assert r.status_code in (401, 403), r.text
    assert sum(calls.values()) == 0


# ── 2. 合理权限不被破坏: member / pro / owner 仍可用 ────────────────────
@pytest.mark.parametrize("who", ["member", "pro", "owner"])
def test_regular_users_can_still_trigger(client, owner_token, member_token, pro_token, calls, who):
    token = {"member": member_token, "pro": pro_token, "owner": owner_token}[who]
    for method, path in WRITE_ENTRY_POINTS:
        r = _call(client, method, path, token)
        assert r.status_code == 200, f"{who} 被误杀: {method} {path} → {r.status_code} {r.text[:200]}"
    assert calls["refresh_signals"] >= 1
    assert calls["refresh_candidates"] >= 1
    assert calls["eval_signals"] == 1
    assert calls["eval_candidates"] == 1


# ── 3. 只读入口保持登录即可 ─────────────────────────────────────────────
@pytest.mark.parametrize(
    "path",
    (
        "/api/recommendations/entry-candidates",
        "/api/recommendations/strategy-signals",
        "/api/recommendations/strategy-signals/refresh-status",
        "/api/recommendations/strategy-catalog",
    ),
)
def test_read_endpoints_stay_open_to_members(client, member_token, path):
    r = client.get(path, headers=_hdr(member_token))
    assert r.status_code == 200, f"只读入口被误收口: {path} → {r.status_code} {r.text[:200]}"


# ── 4. 降权立即生效(实时读库, 不信 JWT claim) ───────────────────────────
def test_demoted_user_loses_write_scope_immediately(client, owner_token, member_token, calls):
    ok = _call(client, "POST", "/api/recommendations/strategy-signals/refresh", member_token)
    assert ok.status_code == 200, ok.text

    _set_role(MEMBER_USER, "guest")
    denied = _call(client, "POST", "/api/recommendations/strategy-signals/refresh", member_token)
    assert denied.status_code == 403, f"降权未生效: {denied.status_code} {denied.text[:200]}"
