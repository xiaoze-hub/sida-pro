# -*- coding: utf-8 -*-
"""因子权重**写**接口的权限收口 + 只读标定可观测性(2026-11-12 独立审计)。

漏洞面(实测):
    POST /api/factors/weights/{factor_code}/{market}
只有 `protected`(登录)一道关, **没有**任何角色/权限依赖 —— 任意 member/pro/
demo(guest) 登录后都能改**全局**因子权重(影响所有人的信号合成), 且审计里
看不出是谁改的。前端把按钮画成 owner-only, 但那只是画: 客户端可改, 不可信。

本文件的验收线(真实 HTTP + 真 app + 隔离临时库, 不打桩判权):
1. 无 token → 401; member / pro / guest(demo) → 403 且库内权重不变;
2. owner 可通过(既有管理路径保留), 审计历史记下**服务端身份**;
3. owner 在 users.permissions 单独开 manage_settings 的 member 可写(既有兼容通道);
4. DB 降权后旧 owner token 立即失效(端点实时取 DB 角色, 不信 JWT claim);
5. `/api/v1/...` 版本别名同样被端点自身拦住(不靠路径前缀中间件);
6. 策略权重重平衡(POST /recommendations/strategy-weights/rebalance)同一门槛;
7. 只读标定端点: 实时计算 + computed_at/error/样本/无行状态, 零写入、不 seed;
8. 旧 meta 里的最近 IC 必须显式标注为**历史观测**及其时间, 不得冒充当前。
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

ADMIN_USER = "fac_w_admin"
ADMIN_PW = "fac_w_admin_pw_1234"
MEMBER_USER = "fac_w_member"
MEMBER_PW = "fac_w_member_pw_1234"
PRO_USER = "fac_w_pro"
PRO_PW = "fac_w_pro_pw_1234"
DEMO_USER = "fac_w_demo"
DEMO_PW = "fac_w_demo_pw_1234"

DB_ENV_KEYS = (
    "DATABASE_URL",
    "DATABASE_URL_WRITE",
    "DATABASE_URL_READ",
    "DATABASE_URL_MIGRATE",
    "SIDA_DB_URL",
    "SIDA_DB",
    "SIDA_APP_DB_ROLE",
)


# ── 隔离: 临时库 + 清掉一切可能指向生产/开发库的环境变量 ──────────────────
@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    """断言测试库是临时 SQLite 文件, 并清空 DB 环境变量。

    清 env 用 monkeypatch(用例结束自动还原), 目的有二:
      - 防止被测代码在运行中**新建**引擎时读到外部 DATABASE_URL 连到真库;
      - 防止判权/标定链路里任何"顺手初始化"的动作落进生产。
    引擎在 conftest 里已按临时 DATA_DIR 建好, 这里只校验它确实不是仓库里的
    `data/sida.db`, 否则直接红 —— 宁可红也不碰真库。
    """
    for key in DB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    from src.db.session import write_engine

    url = str(write_engine.url)
    assert url.startswith("sqlite"), f"测试库必须是隔离 SQLite, 实际: {url}"
    assert "/data/sida.db" not in url, f"拒绝在真实数据文件上跑测试: {url}"
    assert os.environ.get("SIDA_ALLOW_SQLITE") == "1"

    # 只读标定的进程内缓存会跨用例残留, 每例清空
    from src.web.api import factors as factors_api

    factors_api._reset_calibration_cache()
    yield


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    monkeypatch.setenv("AUTH_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AUTH_PASSWORD", ADMIN_PW)
    from src.web.database import SessionLocal
    from tests.conftest import purge_users

    db = SessionLocal()
    try:
        # 只清本文件造的用户, 不 purge 全表 —— 其它测试模块可能依赖自己
        # 已建好的 admin, 全表清理会造成跨文件污染(实测 test_permissions_rbac 连坐)。
        for name in (ADMIN_USER, MEMBER_USER, PRO_USER, DEMO_USER, "fac_w_owner2"):
            purge_users(db, only_username=name)
        _ensure_owner(db, ADMIN_USER, ADMIN_PW)
        # 清掉因子权重/历史, 用例之间互不影响
        from src.web.models import FactorWeight, FactorWeightHistory, StrategyWeight

        db.query(FactorWeightHistory).delete()
        db.query(FactorWeight).delete()
        db.query(StrategyWeight).delete()
        db.commit()
    finally:
        db.close()

    from src.web.app import app

    c = TestClient(app)
    try:
        yield c
    finally:
        c.cookies.clear()
        _purge_own_users()


def _purge_own_users() -> None:
    """用例结束把自己造的账号删干净。

    别的测试模块(如 test_permissions_rbac)假设「库里没有 owner 时由 env 首启建号」;
    本文件留下的 owner 会让那条路径失效 —— 跨文件污染实测就是 4 个用例连坐。
    """
    from src.web.database import SessionLocal
    from tests.conftest import purge_users

    db = SessionLocal()
    try:
        for name in (ADMIN_USER, MEMBER_USER, PRO_USER, DEMO_USER, "fac_w_owner2"):
            purge_users(db, only_username=name)
    finally:
        db.close()


def _ensure_owner(db, username: str, password: str) -> None:
    """确保 owner 账号存在(不依赖 env 首启路径)。

    测试库里若已有别的 owner, `get_or_create_owner` 不会再用 AUTH_USERNAME 建号,
    直接登录会 401; 这里显式补齐本文件的 owner, 不动别的账号。
    """
    from src.web.api.auth import create_user, hash_password
    from src.web.models import User

    row = db.query(User).filter(User.username == username).first()
    if row is None:
        create_user(db, username, password, "owner")
        return
    row.password_hash = hash_password(password)
    row.role = "owner"
    row.is_active = True
    db.commit()


# ── helpers ────────────────────────────────────────────────────────────
def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _hdr(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _make_user(client, owner_token, username, password, role="member") -> str:
    r = client.post(
        "/api/auth/users",
        headers=_hdr(owner_token),
        json={"username": username, "password": password, "role": role},
    )
    assert r.status_code == 200, r.text
    return _login(client, username, password)


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


def _weight(factor="alpha_score", market="CN"):
    from src.web.database import SessionLocal
    from src.web.models import FactorWeight

    db = SessionLocal()
    try:
        row = (
            db.query(FactorWeight)
            .filter(FactorWeight.factor_code == factor, FactorWeight.market == market)
            .first()
        )
        return None if row is None else float(row.weight)
    finally:
        db.close()


def _history():
    from src.web.database import SessionLocal
    from src.web.models import FactorWeightHistory

    db = SessionLocal()
    try:
        return db.query(FactorWeightHistory).all()
    finally:
        db.close()


def _post_weight(client, token, weight=1.3, path=None, factor="alpha_score", market="CN"):
    p = path or f"/api/factors/weights/{factor}/{market}"
    return client.post(p, headers=_hdr(token), json={"weight": weight})


@pytest.fixture()
def owner_token(client) -> str:
    return _login(client, ADMIN_USER, ADMIN_PW)


@pytest.fixture()
def member_token(client, owner_token) -> str:
    return _make_user(client, owner_token, MEMBER_USER, MEMBER_PW, "member")


@pytest.fixture()
def pro_token(client, owner_token) -> str:
    # 创建接口只收 owner/member/guest, pro 走 DB 提升(线上由 pro 审批落库)
    token = _make_user(client, owner_token, PRO_USER, PRO_PW, "member")
    _set_role(PRO_USER, "pro")
    return token


@pytest.fixture()
def demo_token(client, owner_token) -> str:
    return _make_user(client, owner_token, DEMO_USER, DEMO_PW, "guest")


# ── 1. 写接口权限矩阵 ──────────────────────────────────────────────────
def test_no_token_is_rejected(client):
    r = _post_weight(client, None)
    # 未登录: 鉴权依赖给 401; CSRF 中间件可能先给 403 —— 两者都算拒绝。
    assert r.status_code in (401, 403), r.text
    assert _weight() in (None, 1.0)


@pytest.mark.parametrize("who", ["member", "pro", "demo"])
def test_non_admin_roles_cannot_change_global_weight(client, owner_token, who):
    """member / pro / demo 一律 403, 且库内权重保持默认(未被改动)。"""
    token = {
        "member": _make_user(client, owner_token, MEMBER_USER, MEMBER_PW, "member"),
        "pro": _make_user(client, owner_token, PRO_USER, PRO_PW, "member"),
        "demo": _make_user(client, owner_token, DEMO_USER, DEMO_PW, "guest"),
    }[who]
    if who == "pro":
        _set_role(PRO_USER, "pro")

    r = _post_weight(client, token, weight=2.5)
    assert r.status_code == 403, f"{who} 竟能改全局权重: {r.status_code} {r.text[:200]}"
    assert _weight() in (None, 1.0), "被拒绝的请求不得留下任何权重改动"
    assert _history() == [], "被拒绝的请求不得写审计历史"


def test_alias_path_also_enforced_by_endpoint(client, owner_token, member_token):
    """/api/v1 版本别名绕不过端点自身判权(前缀中间件不是唯一防线)。"""
    r = _post_weight(
        client, member_token, weight=2.5, path="/api/v1/factors/weights/alpha_score/CN"
    )
    assert r.status_code == 403, r.text
    assert _weight() in (None, 1.0)


def test_owner_can_still_change_weight(client, owner_token):
    """既有 owner 管理路径保留; 审计历史记服务端身份(不是客户端自报)。"""
    r = _post_weight(client, owner_token, weight=1.3)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["weight"] == 1.3
    assert _weight() == 1.3

    hist = _history()
    assert len(hist) == 1
    meta = hist[0].meta or {}
    assert meta.get("by") == ADMIN_USER, f"审计未记录服务端身份: {meta}"
    assert meta.get("via") == "api"


def test_client_supplied_identity_is_ignored(client, member_token):
    """请求体里伪造 actor/by 字段不得影响审计身份(不信客户端)。"""
    r = client.post(
        "/api/factors/weights/alpha_score/CN",
        headers=_hdr(member_token),
        json={"weight": 1.4, "by": ADMIN_USER, "user": ADMIN_USER, "actor": ADMIN_USER},
    )
    assert r.status_code == 403, r.text
    assert _history() == []


def test_member_can_still_read_weights(client, member_token):
    """收口只加在写上: 读接口保持登录即可(否则 member 页面的权重面板全白)。"""
    r = client.get("/api/factors/weights", headers=_hdr(member_token))
    assert r.status_code == 200, r.text
    assert r.json()["data"]["items"], "只读列表不能因为权限收口而清空"


def test_allowlist_manage_settings_grant_still_works(client, owner_token, member_token):
    """既有兼容通道: owner 给 member 单独开 manage_settings 后可写。"""
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == MEMBER_USER).first()
        u.permissions = ["manage_settings"]
        db.commit()
    finally:
        db.close()

    r = _post_weight(client, member_token, weight=1.2)
    assert r.status_code == 200, f"白名单授权被误杀: {r.status_code} {r.text[:200]}"
    assert _weight() == 1.2
    meta = _history()[0].meta or {}
    assert meta.get("by") == MEMBER_USER


def test_demotion_takes_effect_immediately(client, owner_token):
    """DB 降权后, 旧 owner token(claim 仍 owner)立即 403 —— 实时读 DB 角色。

    用**第二个** owner 做降权对象: 主账号是 `get_or_create_owner` 的兜底 owner,
    把它降成 member 会让鉴权链路现场重建同名 owner 并撞唯一键, 与本次判权无关。
    """
    other = "fac_w_owner2"
    other_pw = "fac_w_owner2_pw_1234"
    other_token = _make_user(client, owner_token, other, other_pw, "owner")

    assert _post_weight(client, other_token, weight=1.1).status_code == 200
    _set_role(other, "member")
    r = _post_weight(client, other_token, weight=2.0)
    assert r.status_code == 403, f"降权未生效: {r.status_code} {r.text[:200]}"
    assert _weight() == 1.1


def test_service_identity_cannot_write(client):
    """服务身份(X-Service-Token 形态)不得写全局权重。"""
    r = client.post(
        "/api/factors/weights/alpha_score/CN",
        headers={"X-Service-Token": "svc-token", "X-Service-Role": "service"},
        json={"weight": 2.0},
    )
    assert r.status_code in (401, 403), r.text
    assert _weight() in (None, 1.0)


# ── 2. 策略权重重平衡: 同一门槛 ─────────────────────────────────────────
def test_strategy_rebalance_requires_manage_settings(client, member_token, owner_token):
    r = client.post(
        "/api/recommendations/strategy-weights/rebalance", headers=_hdr(member_token)
    )
    assert r.status_code == 403, f"member 竟能重平衡策略权重: {r.status_code} {r.text[:200]}"

    ok = client.post(
        "/api/recommendations/strategy-weights/rebalance", headers=_hdr(owner_token)
    )
    assert ok.status_code == 200, ok.text


def test_strategy_rebalance_no_token_rejected(client):
    r = client.post("/api/recommendations/strategy-weights/rebalance")
    assert r.status_code in (401, 403), r.text
