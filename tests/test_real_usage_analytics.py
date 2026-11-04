"""API Key 用量监控（2026-11-04 修复）回归测试。

覆盖本轮修复的五个面：

① **`/api/usage` 路由恢复**：上一版新增 admin analytics 时把它顶掉了（装饰器与
   函数签名被替换，函数体沦为死代码）→ 真实 HTTP 无凭证 401、有效 key 200。
② **管理端权限**：生产严格模式下 owner 的**用户面** token 401；用户 token +
   admin token 200；member 即使拿到 admin token 也 403；手写的
   `uid in ("admin", "1", 1)` 白名单已不存在。
③ **时间口径**：只允许 7 / 30 天；窗口按**自然日连续**（缺日补 0）；窗口首日
   00:00:00 的行计入、前一秒不计入；口径与 `db_now_naive()` 一致（PG 北京 naive /
   SQLite UTC naive 都不会偏移 8 小时）。
④ **IP**：合法 IP 正常计数；非法 / 带端口 / 超长 / 空串 / NULL 统一归 `unknown`
   且该档保留；不可信来源的 XFF 不被采信，可信来源的 XFF 也必须是合法 IP。
⑤ **中文标签**：`OPEN_SKILLS` 的每一个真实键都有中文名；未收录的保留原名并显式
   标注 `（未收录）`。

隔离：所有数据都走 `tests/conftest.py` 的临时 SQLite（conftest 在 import 前把
DATA_DIR / SIDA_DB_URL 指到 mkdtemp），本文件不打开任何外部库；插入的行按 key id
在 fixture 结束时删除。
"""

from __future__ import annotations

import ipaddress
import os
import secrets
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from src.core.client_ip import UNKNOWN_IP, normalize_client_ip
from src.core.skill_labels import (
    SKILL_CHINESE_LABELS,
    UNKNOWN_LABEL_SUFFIX,
    is_known_label,
    label_for,
)
from src.core.timezone import db_now_naive
from src.db.models import SkillApiKey, SkillUsage, User
from src.web.api import skills_gateway as gw
from src.web.database import SessionLocal


@pytest.fixture(scope="module", autouse=True)
def _strict_admin_mode():
    """本文件在**严格模式**（= 生产默认）下跑，否则测不到管理面隔离本身。"""
    old = os.environ.get("STRICT_ADMIN_TOKEN")
    os.environ["STRICT_ADMIN_TOKEN"] = "1"
    yield
    if old is None:
        os.environ.pop("STRICT_ADMIN_TOKEN", None)
    else:
        os.environ["STRICT_ADMIN_TOKEN"] = old


@pytest.fixture(scope="module")
def env():
    """真实 app + 测试库：owner / member 用户、用户 token、admin token、一把可用 key。"""
    from src.core.auth_tokens import create_admin_token, create_token
    from src.web.app import app

    suffix = uuid.uuid4().hex[:8]
    owner_id, member_id = str(uuid.uuid4()), str(uuid.uuid4())
    raw_key = "sk_" + secrets.token_urlsafe(32)

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
        key = SkillApiKey(
            key_hash=gw._hash_key(raw_key),
            key_prefix=raw_key[:11],
            owner_label="usage-test",
            tier="free",
            status="active",
            daily_limit=100,
            user_id=None,
        )
        db.add(key)
        db.commit()
        key_id = key.id
        tok = {
            "owner_user": create_token(owner).token,
            "owner_admin": create_admin_token(owner).token,
            "member_user": create_token(member).token,
            "member_admin": create_admin_token(member).token,
        }
    finally:
        db.close()

    with TestClient(app) as client:
        yield {
            "client": client,
            "tok": tok,
            "key_id": key_id,
            "raw_key": raw_key,
            "owner_id": owner_id,
            "member_id": member_id,
        }

    db = SessionLocal()
    try:
        db.query(SkillUsage).filter(SkillUsage.api_key_id == key_id).delete(synchronize_session=False)
        db.query(SkillApiKey).filter(SkillApiKey.id == key_id).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_([owner_id, member_id])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


# ── 工具 ────────────────────────────────────────────────────────────

def _add_usage(key_id: int, *, skill="get_stock_quote", ip="203.0.113.9",
               at=None, code=200, channel="api") -> int:
    db = SessionLocal()
    try:
        row = SkillUsage(
            api_key_id=key_id, skill_name=skill, status_code=code,
            duration_ms=1, channel=channel, client_ip=ip, created_at=at,
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _db_count(since: datetime, until: datetime | None = None) -> int:
    db = SessionLocal()
    try:
        q = db.query(SkillUsage).filter(SkillUsage.created_at >= since)
        if until is not None:
            q = q.filter(SkillUsage.created_at < until)
        return q.count()
    finally:
        db.close()


def _analytics(env, *, days=7, headers=None):
    h = headers if headers is not None else {
        "Authorization": f"Bearer {env['tok']['owner_user']}",
        "X-Admin-Token": env["tok"]["owner_admin"],
    }
    return env["client"].get("/api/admin/analytics/usage-trends", params={"days": days}, headers=h)


def _data(resp) -> dict:
    """/api/* 被 ResponseWrapperMiddleware 包成 {code, success, data, message}。"""
    body = resp.json()
    if isinstance(body, dict) and "data" in body and "code" in body:
        return body.get("data") or {}
    return body


def _today() -> datetime:
    return db_now_naive().replace(hour=0, minute=0, second=0, microsecond=0)


# ── ① /api/usage 路由恢复 ────────────────────────────────────────────

def test_usage_无凭证401(env):
    r = env["client"].get("/api/usage")
    assert r.status_code == 401, f"/api/usage 无凭证应 401，得到 {r.status_code}: {r.text[:120]}"


def test_usage_无效key401(env):
    r = env["client"].get("/api/usage", headers={"X-API-Key": "sk_" + "0" * 40})
    assert r.status_code == 401


def test_usage_有效key返回当日用量(env):
    """真实 HTTP：有效 X-API-Key → 200，used_today 只数该 key 的今天。"""
    now = db_now_naive()
    today = _today()
    _add_usage(env["key_id"], at=now - timedelta(minutes=1), channel="api")
    _add_usage(env["key_id"], at=now - timedelta(minutes=2), channel="web")
    _add_usage(env["key_id"], at=today - timedelta(minutes=1), channel="api")  # 昨天, 不计

    r = env["client"].get("/api/usage", headers={"X-API-Key": env["raw_key"]})
    assert r.status_code == 200, f"有效 key 被拒: {r.status_code} {r.text[:160]}"
    body = _data(r)
    assert body["used_today"] == 2, body
    assert body["remaining_today"] == body["daily_limit"] - 2
    assert body["by_channel"] == {"api": 1, "web": 1}


# ── ② 管理端权限（严格模式） ─────────────────────────────────────────

def test_analytics_无凭证401(env):
    r = env["client"].get("/api/admin/analytics/usage-trends")
    assert r.status_code == 401, r.text[:160]


def test_analytics_owner的用户token被拒401(env):
    """核心隔离：role=owner 但只有用户面 token —— 必须 401。"""
    r = _analytics(env, headers={"Authorization": f"Bearer {env['tok']['owner_user']}"})
    assert r.status_code == 401, f"用户面 token 触达管理分析: {r.status_code} {r.text[:160]}"


def test_analytics_用户token加admin放行200(env):
    r = _analytics(env)
    assert r.status_code == 200, f"合法双身份被拒: {r.status_code} {r.text[:200]}"
    assert r.json()["success"] is True
    assert _data(r)["days"] == 7


def test_analytics_member加admin也403(env):
    """admin token 不是万能钥匙；id/用户名白名单已被删除，非 owner 一律 403。"""
    r = _analytics(env, headers={
        "Authorization": f"Bearer {env['tok']['member_user']}",
        "X-Admin-Token": env["tok"]["member_admin"],
    })
    assert r.status_code == 403, f"member 提权成功: {r.status_code} {r.text[:160]}"


def test_analytics_不再有id白名单():
    """结构钉子：首版把 "id 恰好是 1 / 用户名恰好叫 admin" 当管理员的判断不能复活。"""
    import inspect
    import re

    src = inspect.getsource(gw.get_admin_usage_analytics)
    # 只检查代码，不检查 docstring（docstring 里会复述旧写法说明为什么删）
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    assert '("admin", "1", 1)' not in code, "提权白名单复活了"
    assert "role" not in code, "权限判断又写回了函数体(应交给 require_owner)"
    assert "Depends(_require_owner)" in src, "管理分析接口没有走 auth.require_owner"


# ── ③ 时间口径 ──────────────────────────────────────────────────────

def test_days只允许7或30(env):
    for bad in (1, 8, 14, 90):
        r = _analytics(env, days=bad)
        assert r.status_code == 400, f"days={bad} 应被拒，得到 {r.status_code}"
    for good in (7, 30):
        r = _analytics(env, days=good)
        assert r.status_code == 200, f"days={good} 被拒: {r.text[:160]}"


def test_窗口按自然日连续(env):
    today = _today()
    for days in (7, 30):
        body = _data(_analytics(env, days=days))
        dates = [d["date"] for d in body["daily_trend"]]
        assert len(dates) == days, f"days={days} 应返回 {days} 个桶, 实际 {len(dates)}"
        assert dates == sorted(dates), "日期序列未按升序"
        assert len(set(dates)) == days, "日期序列有重复"
        assert dates[-1] == today.strftime("%Y-%m-%d"), "末桶必须是今天"
        assert dates[0] == (today - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        assert body["window"] == {"start": dates[0], "end": dates[-1]}


def test_窗口边界按自然日且与库真值一致(env):
    """首日 00:00:00 计入、前一秒不计入；每日桶 = DB 按自然日聚合的真值。

    若日界按 UTC 零点算（首版 bug），首桶会整体左移 8 小时，与库真值对不上。
    """
    day0 = _today() - timedelta(days=6)          # days=7 的第一天
    _add_usage(env["key_id"], at=day0)                                # 边界内
    _add_usage(env["key_id"], at=day0 - timedelta(seconds=1))         # 边界外
    _add_usage(env["key_id"], skill="market_overview", at=day0 + timedelta(hours=12))

    body = _data(_analytics(env, days=7))
    assert body["daily_trend"][0]["total_calls"] == _db_count(day0, day0 + timedelta(days=1))
    assert body["total_calls_in_window"] == _db_count(day0)
    assert body["total_calls_in_window"] == sum(d["total_calls"] for d in body["daily_trend"])


def test_新增key按自然日统计(env):
    body = _data(_analytics(env, days=7))
    assert sum(d["new_keys"] for d in body["daily_trend"]) >= 1  # 本 fixture 刚建了一把 key


def test_qps均值与每秒峰值(env):
    now = db_now_naive()
    burst_second = (now - timedelta(seconds=2)).replace(microsecond=0)
    for _ in range(5):
        _add_usage(env["key_id"], at=burst_second)
    for _ in range(2):
        _add_usage(env["key_id"], at=now - timedelta(seconds=30))

    body = _data(_analytics(env, days=7))
    qps = body["qps"]
    # 均值 = 计数/秒数（不是"最近一条的瞬时值"）
    assert qps["current_10s"] == round(qps["samples_10s"] / 10.0, 2)
    assert qps["avg_60s"] == round(qps["samples_60s"] / 60.0, 2)
    assert qps["samples_10s"] >= 5
    assert qps["samples_60s"] >= 7
    assert qps["peak_per_sec"] >= 5, f"每秒峰值漏掉了同秒突刺: {qps}"
    assert qps["peak_second"] is not None


# ── ④ IP ────────────────────────────────────────────────────────────

def test_ip_合法才算超长归unknown且保留该档(env):
    _add_usage(env["key_id"], ip="203.0.113.7", skill="ip_probe_a")
    _add_usage(env["key_id"], ip="203.0.113.7", skill="ip_probe_a")
    _add_usage(env["key_id"], ip="not-an-ip", skill="ip_probe_b")
    _add_usage(env["key_id"], ip="203.0.113.8:8080", skill="ip_probe_b")
    _add_usage(env["key_id"], ip="x" * 80, skill="ip_probe_b")
    _add_usage(env["key_id"], ip="", skill="ip_probe_b")
    _add_usage(env["key_id"], ip=None, skill="ip_probe_b")

    body = _data(_analytics(env, days=7))
    ips = {e["ip"]: e["count"] for e in body["top_ips"]}

    assert ips.get("203.0.113.7", 0) >= 2
    assert ips.get(UNKNOWN_IP, 0) >= 4, f"脏 IP 应归入 unknown 并保留该档: {ips}"
    # 不变量：报表里的每一条要么是合法 IP，要么是 unknown
    for ip in ips:
        assert ip == UNKNOWN_IP or ipaddress.ip_address(ip)
        assert len(ip) <= 64
    assert body["unique_ips"] >= len(ips)


def test_不可信来源的XFF不被采信():
    from src.web.middleware import _get_client_ip

    def req(headers: dict, host: str) -> StarletteRequest:
        return StarletteRequest({
            "type": "http", "method": "GET", "path": "/", "raw_path": b"/",
            "query_string": b"", "scheme": "http", "server": ("test", 80),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (host, 1234),
        })

    # 公网 peer + 伪造 XFF → 用 peer
    assert _get_client_ip(req({"x-forwarded-for": "1.2.3.4"}, "8.8.8.8")) == "8.8.8.8"
    # 可信反代 peer + 合法 XFF → 取首段
    assert _get_client_ip(req({"x-forwarded-for": "203.0.113.5, 10.0.0.1"}, "127.0.0.1")) == "203.0.113.5"
    # 可信反代 peer + 非法/超长 XFF → 回落 peer, 绝不把垃圾当 IP
    assert _get_client_ip(req({"x-forwarded-for": "<script>x</script>"}, "127.0.0.1")) == "127.0.0.1"
    assert _get_client_ip(req({"x-forwarded-for": "x" * 100}, "127.0.0.1")) == "127.0.0.1"


def test_ip规范化():
    assert normalize_client_ip(" 203.0.113.9 ") == "203.0.113.9"
    assert normalize_client_ip("2001:0db8:0:0:0:0:0:1") == "2001:db8::1"
    assert normalize_client_ip(None) == UNKNOWN_IP
    assert normalize_client_ip("") == UNKNOWN_IP
    assert normalize_client_ip("unknown") == UNKNOWN_IP
    assert normalize_client_ip("1.2.3.4:8080") == UNKNOWN_IP
    assert len(normalize_client_ip("x" * 100)) < 10


# ── ⑤ 中文标签 ──────────────────────────────────────────────────────

def test_所有开放skill都有中文标签():
    from src.web.api.skills_gateway import OPEN_SKILLS

    missing = sorted(set(OPEN_SKILLS) - set(SKILL_CHINESE_LABELS))
    assert not missing, f"这些开放接口没有中文标签: {missing}"
    for name in OPEN_SKILLS:
        assert label_for(name) == SKILL_CHINESE_LABELS[name]
        assert is_known_label(name) is True


def test_未收录保留原名并标注未知():
    name = "brand_new_skill"
    label = label_for(name)
    assert name in label, "未收录时必须保留原名"
    assert label.endswith(UNKNOWN_LABEL_SUFFIX), label
    assert is_known_label(name) is False
    assert label_for("") == "未知接口"


def test_top_skills带中文名与已知标记(env):
    _add_usage(env["key_id"], skill="get_stock_quote")
    _add_usage(env["key_id"], skill="totally_unknown_skill")
    body = _data(_analytics(env, days=7))
    by_name = {e["skill_name"]: e for e in body["top_skills"]}
    assert by_name["get_stock_quote"]["display_name"] == SKILL_CHINESE_LABELS["get_stock_quote"]
    assert by_name["get_stock_quote"]["known"] is True
    assert by_name["totally_unknown_skill"]["known"] is False
    assert "totally_unknown_skill" in by_name["totally_unknown_skill"]["display_name"]
