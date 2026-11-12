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
   收口到 manage_settings(受限管理员), 并发范围如实报 "process"(无跨 worker 承诺),
   失败只回安全文案 + 关联号(异常原文/SQL/DSN 不进响应);
8. 旧 meta 里的最近 IC 必须显式标注为**历史观测**及其时间, 不得冒充当前。
"""
from __future__ import annotations

import os
import time

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
        # 清掉因子权重/历史/策略权重, 用例之间互不影响
        from src.web.models import (
            AuditLog,
            FactorWeight,
            FactorWeightHistory,
            StrategyWeight,
        )

        db.query(FactorWeightHistory).delete()
        db.query(FactorWeight).delete()
        db.query(StrategyWeight).delete()
        # 本文件新增的审计行也逐例清掉, 否则计数会被上一例残留污染。
        db.query(AuditLog).filter(
            AuditLog.action.in_(
                ("update_factor_weight", "rebalance_strategy_weights")
            )
        ).delete(synchronize_session=False)
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


# ── 3. 人工改动的持久审计(pin / auto_calibrate 不得混进权重历史快照) ──────
def _audit_rows(action: str | None = None):
    from src.web.database import SessionLocal
    from src.web.models import AuditLog

    db = SessionLocal()
    try:
        q = db.query(AuditLog)
        if action:
            q = q.filter(AuditLog.action == action)
        return q.order_by(AuditLog.id.asc()).all()
    finally:
        db.close()


def test_pin_and_auto_calibrate_changes_are_audited(client, owner_token):
    """pin/auto_calibrate 改动必须留持久审计, 且**不写** FactorWeightHistory。"""
    import json

    first = client.post(
        "/api/factors/weights/alpha_score/CN",
        headers=_hdr(owner_token),
        json={"is_pinned": True},
    )
    assert first.status_code == 200, first.text
    second = client.post(
        "/api/factors/weights/alpha_score/CN",
        headers=_hdr(owner_token),
        json={"auto_calibrate": False},
    )
    assert second.status_code == 200, second.text

    # 权重历史是时间快照, 只允许真权重变化; 开关翻转不许污染 as_of 回放。
    assert _history() == []

    rows = _audit_rows("update_factor_weight")
    assert len(rows) == 2, [r.action for r in _audit_rows()]
    assert all(r.username == ADMIN_USER for r in rows)
    details = [json.loads(r.detail) for r in rows]
    assert details[0]["changes"]["is_pinned"] == {"before": False, "after": True}
    assert details[1]["changes"]["auto_calibrate"] == {"before": True, "after": False}
    assert all(d["via"] == "api" for d in details)


def test_weight_change_audit_is_same_transaction_as_history(client, owner_token):
    """权重变化: 历史行 + 审计行同时落地, 审计可追到人。"""
    r = client.post(
        "/api/factors/weights/alpha_score/CN",
        headers=_hdr(owner_token),
        json={"weight": 1.25},
    )
    assert r.status_code == 200, r.text
    hist = _history()
    assert len(hist) == 1 and hist[0].new_weight == 1.25
    rows = _audit_rows("update_factor_weight")
    assert len(rows) == 1
    assert rows[0].username == ADMIN_USER


def test_strategy_rebalance_writes_audit_with_actor(client, owner_token):
    r = client.post(
        "/api/recommendations/strategy-weights/rebalance", headers=_hdr(owner_token)
    )
    assert r.status_code == 200, r.text
    rows = _audit_rows("rebalance_strategy_weights")
    assert len(rows) == 1
    assert rows[0].username == ADMIN_USER
    assert "window_days" in rows[0].detail


# ── 4. 只读标定的并发/缓存安全 ───────────────────────────────────────────
def _calib(client, token, path="/api/factors/calibration?market=CN"):
    return client.get(path, headers=_hdr(token))


def test_calibration_error_is_not_cached(client, owner_token, monkeypatch):
    """失败结果不得进缓存: 下一次请求必须真的重算, 而不是复读错误。"""
    from src.core import factor_calibration as fc
    from src.web.api import factors as factors_api

    monkeypatch.setattr(
        fc, "evaluate_factor_ic",
        lambda **kw: {"factors": {}, "error": "boom"},
    )
    bad = _calib(client, owner_token)
    assert bad.status_code == 200, bad.text
    assert bad.json()["data"]["ok"] is False

    monkeypatch.setattr(
        fc, "evaluate_factor_ic",
        lambda **kw: {"factors": {}, "error": None},
    )
    monkeypatch.setattr(factors_api, "_CALIB_LAST_COMPUTE", 0.0)
    good = _calib(client, owner_token)
    assert good.status_code == 200, good.text
    assert good.json()["data"]["ok"] is True
    assert good.json()["data"]["cached"] is False


def test_calibration_single_flight_rejects_concurrent_compute(
    client, owner_token, monkeypatch
):
    """同一时刻只允许一轮实际计算; 并发请求立即 429, 不叠线程池。"""
    import threading

    from src.core import factor_calibration as fc
    from src.web.api import factors as factors_api

    started = threading.Event()
    release = threading.Event()
    real = fc.evaluate_factor_ic

    def _slow(**kwargs):
        started.set()
        assert release.wait(10), "测试未释放慢计算"
        return real(**kwargs)

    monkeypatch.setattr(fc, "evaluate_factor_ic", _slow)

    errors: list[BaseException] = []

    def _run_first():
        try:
            _calib(client, owner_token, "/api/factors/calibration?market=CN&days=30")
        except BaseException as e:  # pragma: no cover - 线程里只记录
            errors.append(e)

    t = threading.Thread(target=_run_first)
    t.start()
    try:
        assert started.wait(10), "首轮计算未启动"
        deadline = 50
        while not getattr(factors_api, "_CALIB_INFLIGHT", None) and deadline:
            time.sleep(0.02)
            deadline -= 1
        assert factors_api._CALIB_INFLIGHT, "未登记 single-flight 状态"

        second = _calib(client, owner_token, "/api/factors/calibration?market=HK")
        assert second.status_code == 429, f"并发未拦截: {second.status_code}"
        assert "进行中" in second.text or "频繁" in second.text
    finally:
        release.set()
        t.join(20)
    assert not errors, errors


def test_calibration_cache_is_bounded(client, owner_token, monkeypatch):
    """参数有界 + 缓存条目上限: 换参数不能无限堆内存。"""
    from src.core import factor_calibration as fc
    from src.web.api import factors as factors_api

    monkeypatch.setattr(
        fc, "evaluate_factor_ic", lambda **kw: {"factors": {}, "error": None}
    )
    monkeypatch.setattr(factors_api, "_CALIB_CACHE_MAX_ENTRIES", 2)

    for days in (30, 40, 50):
        monkeypatch.setattr(factors_api, "_CALIB_LAST_COMPUTE", 0.0)
        r = _calib(client, owner_token, f"/api/factors/calibration?market=CN&days={days}")
        assert r.status_code == 200, r.text

    assert len(factors_api._CALIB_CACHE) <= 2


def test_calibration_cache_does_not_serve_stale_current_weight(
    client, owner_token, monkeypatch
):
    """缓存不得让 current_weight 停留在旧值: 人工改权重后必须反映新值。"""
    from src.core import factor_calibration as fc

    monkeypatch.setattr(
        fc, "evaluate_factor_ic", lambda **kw: {"factors": {}, "error": None}
    )

    first = _calib(client, owner_token).json()["data"]
    alpha = next(p for p in first["proposals"] if p["factor_code"] == "alpha_score")
    assert alpha["current_weight"] == 1.0

    r = client.post(
        "/api/factors/weights/alpha_score/CN",
        headers=_hdr(owner_token),
        json={"weight": 1.4},
    )
    assert r.status_code == 200, r.text

    second = _calib(client, owner_token).json()["data"]
    alpha2 = next(p for p in second["proposals"] if p["factor_code"] == "alpha_score")
    assert alpha2["current_weight"] == 1.4, "缓存回放了陈旧 current_weight"
    assert second["cached"] is False


def test_calibration_cached_payload_is_isolated_per_caller(monkeypatch):
    """缓存命中返回深拷贝: 一个调用方改返回值不能污染其他用户看到的建议。"""
    from src.core import factor_calibration as fc
    from src.web.api import factors as factors_api
    from src.web.database import SessionLocal

    monkeypatch.setattr(
        fc, "evaluate_factor_ic", lambda **kw: {"factors": {}, "error": None}
    )
    factors_api._reset_calibration_cache()
    db = SessionLocal()
    try:
        first = factors_api.calibration(
            market="CN", days=90, horizon=5, min_samples=20, db=db
        )
        n = len(first["proposals"])
        first["proposals"].append({"factor_code": "tampered"})
        first["proposals"][0]["current_weight"] = 9.99

        second = factors_api.calibration(
            market="CN", days=90, horizon=5, min_samples=20, db=db
        )
        assert second["cached"] is True
        assert len(second["proposals"]) == n
        assert second["proposals"][0]["current_weight"] != 9.99
    finally:
        db.close()


# ── 5. 策略调权失败必须反映在 HTTP 状态, 且整笔回滚 ───────────────────────
def _strategy_snapshot():
    from src.web.database import SessionLocal
    from src.web.models import StrategyWeight, StrategyWeightHistory

    db = SessionLocal()
    try:
        return (
            db.query(StrategyWeight).count(),
            db.query(StrategyWeightHistory).count(),
        )
    finally:
        db.close()


def test_rebalance_failure_returns_500_and_rolls_back(client, owner_token, monkeypatch):
    """核心函数把异常收敛成 error 字段; HTTP 层必须 500, 不能回 200 假装成功。

    同时验证回滚: 权重行/历史行/审计行都不许留下半成品。
    """
    from src.core import strategy_engine as se

    before = _strategy_snapshot()

    def _boom(*args, **kwargs):
        raise RuntimeError("catalog 读取失败")

    monkeypatch.setattr(se, "list_strategy_catalog", _boom)

    r = client.post(
        "/api/recommendations/strategy-weights/rebalance", headers=_hdr(owner_token)
    )
    assert r.status_code == 500, f"调权失败却回了 {r.status_code}: {r.text[:300]}"
    # 失败原因不再原样回显(可能带 SQL/表名/DSN), 只给安全文案 + 关联号。
    assert "catalog 读取失败" not in r.text, "异常原文泄漏到响应里"
    assert "ERR-" in r.text, "安全文案必须带关联号"
    assert "回滚" in r.text, "响应没有说明已回滚"

    assert _strategy_snapshot() == before, "失败路径留下了权重/历史改动"
    assert _audit_rows("rebalance_strategy_weights") == [], "失败路径不该留下「成功」审计"


def test_rebalance_denied_requests_leave_no_rows(client, member_token):
    """member 403 之后, 权重表/历史/审计都必须干净(权限拒绝没有副作用)。"""
    before = _strategy_snapshot()
    r = client.post(
        "/api/recommendations/strategy-weights/rebalance", headers=_hdr(member_token)
    )
    assert r.status_code == 403, r.text
    assert _strategy_snapshot() == before
    assert _audit_rows("rebalance_strategy_weights") == []
# ── 6. 只读标定: 受限管理员 + 如实上报「只有进程内单飞」 ──────────────────
#
# 2026-11-12 复审: 之前给标定加过一层 Redis 租约并宣称「跨 worker 互斥」, 但那层
# 没有续租、Redis 抖动时静默降级, 给不出全链路独占。现在租约已移除: 端点收口到
# manage_settings(受限管理员) + 参数上限 + 墙钟预算, 并发范围如实写 "process"。


def _stub_calibration(monkeypatch, calls: dict | None = None):
    from src.core import factor_calibration as fc

    def _fake(**kwargs):
        if calls is not None:
            calls["n"] = calls.get("n", 0) + 1
        return {"factors": {}, "error": None}

    monkeypatch.setattr(fc, "evaluate_factor_ic", _fake)


def test_calibration_requires_manage_settings(client, member_token, owner_token, monkeypatch):
    """member 403 且**不触发**扫库; owner 才真正算一轮。"""
    calls: dict = {"n": 0}
    _stub_calibration(monkeypatch, calls)

    denied = _calib(client, member_token)
    assert denied.status_code == 403, f"member 竟能触发只读重查询: {denied.status_code}"
    assert calls["n"] == 0, "被拒绝的请求不得开始扫库"

    ok = _calib(client, owner_token)
    assert ok.status_code == 200, ok.text
    assert calls["n"] == 1


def test_calibration_no_token_rejected(client):
    assert _calib(client, None).status_code in (401, 403)


def test_calibration_reports_process_scope_only(client, owner_token, monkeypatch):
    """并发范围必须如实: 只有本进程 single-flight, 没有跨 worker/全局互斥。"""
    _stub_calibration(monkeypatch)
    r = _calib(client, owner_token)
    assert r.status_code == 200, r.text
    conc = r.json()["data"]["concurrency"]
    assert conc["scope"] == "process"
    assert conc["cross_worker_exclusive"] is False, "不得宣称跨 worker 互斥"
    assert conc["cache_scope"] == "process", "缓存是每 worker 一份"
    assert "多 worker" in conc["note"], "必须点明多 worker 下的真实并发"


def test_calibration_error_hides_sql_and_dsn(client, owner_token, monkeypatch):
    """失败文案只含关联号: SQL / 表名 / DSN 凭证一律不回给客户端。"""
    from src.core import factor_calibration as fc

    sql = "SELECT weight FROM factor_weights WHERE market='CN'"
    dsn = "postgresql://sida:sup3r-secret@db.internal:5432/sida"

    def _boom(**kwargs):
        raise RuntimeError(f"{sql} 失败; dsn={dsn}")

    monkeypatch.setattr(fc, "evaluate_factor_ic", _boom)
    r = _calib(client, owner_token)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["ok"] is False

    msg = data.get("error") or ""
    for leak in (sql, "factor_weights", "sup3r-secret", "postgresql://", "db.internal"):
        assert leak not in msg, f"错误文案泄漏内部信息: {leak} 出现在 {msg!r}"
    assert data["error_ref"].startswith("ERR-")
    assert data["error_ref"] in msg, "安全文案必须带关联号, 否则无法与日志对账"


def test_calibration_time_budget_aborts_explicitly(monkeypatch):
    """墙钟预算超了就要如实中止(未写入), 不能挂着不返回。"""
    import time as _time

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import src.web.models  # noqa: F401
    from src.core import factor_calibration as fc
    from src.web.database import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    def _slow(**kwargs):
        _time.sleep(0.02)
        return {"factors": {}, "error": None}

    monkeypatch.setattr(fc, "evaluate_factor_ic", _slow)
    try:
        res = fc.calibrate_factor_weights("CN", db=db, time_budget_seconds=0.001)
        assert res["ok"] is False
        assert res.get("timeout") is True
        assert "时间预算" in res["error"]
        assert res["proposals"] == []
        assert res["write_back"] is False
    finally:
        db.close()
