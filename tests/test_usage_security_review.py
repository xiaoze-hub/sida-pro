"""API Key 用量监控**复核**（2026-11-04 接管集成后的第二轮修复）回归测试。

本轮修的是上一轮修复留下的五个面：

① **管理面鉴权**：`skills_gateway._require_owner_admin` 原来是
   `Depends(get_current_user)` + 手判 `role == "owner"` —— 生产严格模式下 owner 的
   **用户面** token 就能读全局 Key 清单、调额度、看全局用量/耗时。现在复用
   `auth.require_owner`（严格模式 = 独立 admin 受众 token），对 `keys`、`usage`、
   `latency`、`analytics` 四类原调用点跑同一套矩阵：owner 用户 token 401 /
   用户 token + admin token 200 / member + admin 403 / 无凭证 401。

② **XFF 信任边界**：旧实现取 `X-Forwarded-For` **首段**，而首段正是客户端自己发的
   那一段。仓库里没有 nginx 配置、无法证明生产用的是覆盖式 `$remote_addr`，所以按
   最常见的追加式（`$proxy_add_x_forwarded_for`）反推：从右往左、跳过我们自己的
   内网跳，取第一个不可信地址。测试覆盖追加式/覆盖式/多级可信反代/脏段/超长链/
   公网直连（XFF 一概不认，全局限流行为不变）。

③ **client_ip 规范化**：拒绝 IPv6 scope id（`fe80::1%eth0`，`ipaddress` 会原样带出
   客户端可写的 zone 名），IPv4-mapped IPv6 归一成 IPv4（否则同一来源被算成两个）。

④ **unique_ips 准确性**：旧实现 SQL `limit(200)` 后把 `len(ip_totals)` 当唯一 IP
   总数 —— 窗口内来源超过 200 时数字恒定在 200 附近但**是错的**。现在全量归并
   （上限 `_USAGE_UNIQUE_IP_CAP`），超限返回 `unique_ips=null` +
   `unique_ips_exact=false` + `top_ips_truncated=true`，不拿采样冒充准确值。

⑤ **额度口径一致性**：`_check_rate_limit` 按 UTC 自然日扣额度，`/api/usage`、
   `api_keys` 单 key 用量却按北京零点数 —— 北京 00:00-08:00 展示的 used_today 偏小、
   remaining_today 偏大。额度语义**不动**（改日界会让切换当天额度翻倍），把展示侧
   统一到 `quota_day_start_naive()`，并用冻结时钟测北京 00:00-08:00。

隔离：临时 SQLite（conftest），本文件只用自己 fixture 建的 key；每条精确计数用例
先清掉这把 key 的历史行，结束后整体删除。
"""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from src.core.client_ip import UNKNOWN_IP, normalize_client_ip
from src.core.timezone import db_naive_from_utc
from src.db.models import SkillApiKey, SkillUsage, User
from src.web.api import skills_gateway as gw
from src.web.database import SessionLocal

ADMIN_ENDPOINTS = (
    "/api/admin/skills/keys",
    "/api/admin/skills/usage/latency",
    "/api/admin/skills/usage",
    "/api/admin/analytics/usage-trends",
)

#: 北京 2026-11-04 00:30 = UTC 2026-11-03 16:30（配额仍属 UTC 11-03）。
BEIJING_EARLY_MORNING = datetime(2026, 11, 3, 16, 30, tzinfo=timezone.utc)


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
    from src.core.auth_tokens import create_admin_token, create_token
    from src.web.app import app

    suffix = uuid.uuid4().hex[:8]
    owner_id, member_id = str(uuid.uuid4()), str(uuid.uuid4())
    raw_key = "sk_" + secrets.token_urlsafe(32)

    db = SessionLocal()
    try:
        owner = User(
            id=owner_id, username=f"sro_{suffix}", password_hash="x",
            role="owner", is_active=True, token_version=1,
        )
        member = User(
            id=member_id, username=f"srm_{suffix}", password_hash="x",
            role="member", is_active=True, token_version=1,
        )
        db.add_all([owner, member])
        db.commit()
        key = SkillApiKey(
            key_hash=gw._hash_key(raw_key),
            key_prefix=raw_key[:11],
            owner_label="security-review",
            tier="free",
            status="active",
            daily_limit=100,
            user_id=owner_id,
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

def _add_usage(key_id: int, *, ip="203.0.113.9", at=None, skill="get_stock_quote",
               channel="api") -> int:
    db = SessionLocal()
    try:
        row = SkillUsage(
            api_key_id=key_id, skill_name=skill, status_code=200,
            duration_ms=1, channel=channel, client_ip=ip, created_at=at,
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _clear_usage(key_id: int) -> None:
    db = SessionLocal()
    try:
        db.query(SkillUsage).filter(SkillUsage.api_key_id == key_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _data(resp) -> dict:
    """/api/* 被 ResponseWrapperMiddleware 包成 {code, success, data, message}。"""
    body = resp.json()
    if isinstance(body, dict) and "data" in body and "code" in body:
        return body.get("data") or {}
    return body


def _admin_headers(env, kind: str) -> dict:
    if kind == "none":
        return {}
    if kind == "owner_user":
        return {"Authorization": f"Bearer {env['tok']['owner_user']}"}
    if kind == "dual":
        return {
            "Authorization": f"Bearer {env['tok']['owner_user']}",
            "X-Admin-Token": env["tok"]["owner_admin"],
        }
    if kind == "member":
        return {
            "Authorization": f"Bearer {env['tok']['member_user']}",
            "X-Admin-Token": env["tok"]["member_admin"],
        }
    raise AssertionError(kind)


def _req(headers: dict, host: str) -> StarletteRequest:
    return StarletteRequest({
        "type": "http", "method": "GET", "path": "/", "raw_path": b"/",
        "query_string": b"", "scheme": "http", "server": ("test", 80),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (host, 1234),
    })


def _client_ip(headers: dict, host: str) -> str:
    from src.web.middleware import _get_client_ip

    return _get_client_ip(_req(headers, host))


# ── ① 管理面鉴权：四类原调用点同一矩阵 ───────────────────────────────

@pytest.mark.parametrize("path", ADMIN_ENDPOINTS)
def test_admin_无凭证401(env, path):
    r = env["client"].get(path)
    assert r.status_code == 401, f"{path} 无凭证放行: {r.status_code} {r.text[:160]}"


@pytest.mark.parametrize("path", ADMIN_ENDPOINTS)
def test_admin_owner用户面token被拒401(env, path):
    """核心隔离：role=owner 但只有用户面 token —— 必须 401。"""
    r = env["client"].get(path, headers=_admin_headers(env, "owner_user"))
    assert r.status_code == 401, f"{path} 用户面 token 触达管理端点: {r.status_code} {r.text[:160]}"


@pytest.mark.parametrize("path", ADMIN_ENDPOINTS)
def test_admin_用户token加admin放行200(env, path):
    r = env["client"].get(path, headers=_admin_headers(env, "dual"))
    assert r.status_code == 200, f"{path} 合法双身份被拒: {r.status_code} {r.text[:200]}"


@pytest.mark.parametrize("path", ADMIN_ENDPOINTS)
def test_admin_member加admin也403(env, path):
    r = env["client"].get(path, headers=_admin_headers(env, "member"))
    assert r.status_code == 403, f"{path} member 提权成功: {r.status_code} {r.text[:160]}"


def test_admin_四个端点都走require_owner():
    """结构钉子：不许再加第二个"看起来像管理员"的依赖。"""
    import inspect

    from src.web.api import auth

    assert gw._require_owner_admin is auth.require_owner
    src = inspect.getsource(gw)
    assert "if user.role != \"owner\"" not in src


# ── ② XFF 信任边界 ──────────────────────────────────────────────────

def test_xff_追加式取真实来源而非客户端伪造的首段():
    """nginx `$proxy_add_x_forwarded_for`：伪造值在左，真实值由反代追加在右。"""
    assert _client_ip({"x-forwarded-for": "1.2.3.4, 203.0.113.9"}, "127.0.0.1") == "203.0.113.9"
    # 客户端伪造多个值也一样
    assert _client_ip({"x-forwarded-for": "1.2.3.4, 5.6.7.8, 203.0.113.9"}, "127.0.0.1") == "203.0.113.9"


def test_xff_覆盖式单值与旧行为一致():
    """nginx 用 `$remote_addr` 覆盖时只有一个值，行为不变（不改全局限流口径）。"""
    assert _client_ip({"x-forwarded-for": "203.0.113.9"}, "127.0.0.1") == "203.0.113.9"


def test_xff_跳过可信反代链():
    assert _client_ip(
        {"x-forwarded-for": "1.2.3.4, 203.0.113.9, 10.0.0.7, 172.16.0.8"}, "127.0.0.1"
    ) == "203.0.113.9"


def test_xff_脏段跳过而不是整条链放弃():
    assert _client_ip({"x-forwarded-for": "203.0.113.9, <script>x</script>"}, "127.0.0.1") == "203.0.113.9"
    assert _client_ip({"x-forwarded-for": "203.0.113.9, 999.1.1.1"}, "127.0.0.1") == "203.0.113.9"


def test_xff_公网直连一概不认():
    """裸跑/非可信 peer：XFF 完全不采信 —— 全局限流仍按 peer 分桶。"""
    assert _client_ip({"x-forwarded-for": "203.0.113.9"}, "8.8.8.8") == "8.8.8.8"


def test_xff_全内网链取最近一跳():
    """内网调用方：没有不可信跳时退回最右一跳（反代追加的那个），不取最左可伪造段。"""
    assert _client_ip({"x-forwarded-for": "10.1.2.3, 10.9.9.9"}, "127.0.0.1") == "10.9.9.9"


def test_xff_CGNAT不算自有反代():
    """运营商 NAT 地址不是我们的反代：不能被跳过，否则会退回左侧伪造值。"""
    assert _client_ip({"x-forwarded-for": "1.2.3.4, 100.64.5.6"}, "127.0.0.1") == "100.64.5.6"


def test_xff_超长链只看右侧且结果稳定():
    padding = ", ".join(f"1.1.1.{i}" for i in range(1, 40))
    assert _client_ip({"x-forwarded-for": f"{padding}, 203.0.113.9"}, "127.0.0.1") == "203.0.113.9"


def test_xff_scope_id不被采信_mapped归一():
    assert _client_ip({"x-forwarded-for": "fe80::1%eth0, 203.0.113.9"}, "127.0.0.1") == "203.0.113.9"
    assert _client_ip({"x-forwarded-for": "::ffff:203.0.113.9"}, "127.0.0.1") == "203.0.113.9"


def test_xff_peer非IP时unknown():
    assert _client_ip({"x-forwarded-for": "203.0.113.9"}, "testclient") == UNKNOWN_IP


# ── ③ client_ip 规范化 ──────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("fe80::1%eth0", UNKNOWN_IP),
    ("fe80::1%25eth0", UNKNOWN_IP),
    ("::1%lo", UNKNOWN_IP),
    ("::ffff:203.0.113.9", "203.0.113.9"),
    ("::FFFF:203.0.113.9", "203.0.113.9"),
    ("2001:0DB8:0:0:0:0:0:1", "2001:db8::1"),
    (" 203.0.113.9 ", "203.0.113.9"),
    ("203.0.113.9:8080", UNKNOWN_IP),
    (None, UNKNOWN_IP),
])
def test_client_ip规范化(raw, expected):
    assert normalize_client_ip(raw) == expected


def test_scope_id不能当长度旁路():
    """zone 名不受地址格式约束 —— 只要带 `%` 就不认，别指望列宽兜底。"""
    assert normalize_client_ip("fe80::1%" + "a" * 40) == UNKNOWN_IP


# ── ④ unique_ips 准确性 ─────────────────────────────────────────────

def _analytics(env, days=7) -> dict:
    r = env["client"].get(
        f"/api/admin/analytics/usage-trends?days={days}",
        headers=_admin_headers(env, "dual"),
    )
    assert r.status_code == 200, r.text[:200]
    return _data(r)


def test_unique_ips精确且合并mapped写法(env):
    _clear_usage(env["key_id"])
    _add_usage(env["key_id"], ip="203.0.113.9")
    _add_usage(env["key_id"], ip="::ffff:203.0.113.9")   # 同一来源的另一种写法
    _add_usage(env["key_id"], ip="2001:db8::9")

    body = _analytics(env)
    assert body["unique_ips_exact"] is True
    assert body["top_ips_truncated"] is False
    assert body["unique_ips"] == 2, body["top_ips"]      # 203.0.113.9 合并后 + 1 个 v6
    ips = {row["ip"]: row["count"] for row in body["top_ips"]}
    assert ips["203.0.113.9"] == 2
    assert "::ffff:203.0.113.9" not in ips


def test_unique_ips超上限时不冒充准确值(env, monkeypatch):
    _clear_usage(env["key_id"])
    for i in range(3):
        _add_usage(env["key_id"], ip=f"203.0.113.{10 + i}")

    # 把扫描上限压到 2：3 个不同原始值 > 上限 → 不允许返回"看起来准确"的 2/3
    monkeypatch.setattr(gw, "_USAGE_UNIQUE_IP_CAP", 2)
    body = _analytics(env)
    assert body["unique_ips"] is None
    assert body["unique_ips_exact"] is False
    assert body["top_ips_truncated"] is True
    assert body["top_ips"], "截断也要给出采样 Top IP，不能整块消失"


# ── ⑤ 额度口径：配额日界 = 展示日界（冻结在北京 00:00-08:00） ───────

def test_配额日键按UTC():
    assert gw.quota_day_key(BEIJING_EARLY_MORNING) == "20261103"
    key, start = gw.quota_day_start_naive(BEIJING_EARLY_MORNING)
    assert key == "20261103"
    # 测试库是 SQLite → 列时钟是 UTC naive，日界就是 UTC 00:00
    assert start == datetime(2026, 11, 3, 0, 0)


def test_PG列时钟下日界是北京08点(monkeypatch):
    """PG 的 created_at 是北京 naive：UTC 00:00 必须换算成北京 08:00 才能比。"""
    monkeypatch.setenv("DATABASE_URL_WRITE", "postgresql://u:p@127.0.0.1:5432/db")
    monkeypatch.delenv("SIDA_DB_URL", raising=False)
    assert db_naive_from_utc(datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc)) == datetime(2026, 11, 3, 8, 0)
    _, start = gw.quota_day_start_naive(BEIJING_EARLY_MORNING)
    assert start == datetime(2026, 11, 3, 8, 0)


def test_限流器与展示用同一个日键(monkeypatch):
    """`_check_rate_limit` 的 Redis 日键必须由 `quota_day_key` 决定。"""
    captured: list[str] = []

    class FakeRedis:
        def incr(self, key):
            captured.append(key)
            return 1

        def expire(self, key, ttl):
            return True

    monkeypatch.setattr(gw, "_quota_now_utc", lambda: BEIJING_EARLY_MORNING)
    monkeypatch.setattr(gw, "_redis_client", lambda: FakeRedis())
    gw._check_rate_limit("review_hash_for_day_key", 100, tier="pro", minute_limit=600)
    assert captured and captured[0].endswith(":20261103"), captured


def test_北京00到08点展示用量与配额同窗口(env, monkeypatch):
    """北京 00:30：配额窗口从北京前一天 08:00 起算，展示必须一致。"""
    _clear_usage(env["key_id"])
    monkeypatch.setattr(gw, "_quota_now_utc", lambda: BEIJING_EARLY_MORNING)
    _, day_start = gw.quota_day_start_naive(BEIJING_EARLY_MORNING)

    _add_usage(env["key_id"], at=day_start - timedelta(minutes=1), channel="api")  # 窗口外
    _add_usage(env["key_id"], at=day_start + timedelta(minutes=1), channel="api")  # 窗口内

    r = env["client"].get("/api/usage", headers={"X-API-Key": env["raw_key"]})
    assert r.status_code == 200, r.text[:200]
    body = _data(r)
    assert body["quota_day"] == "20261103"
    assert body["used_today"] == 1, body
    assert body["remaining_today"] == 99

    # 单 key 用量（api_keys.py）与"我的 key 用量"（skills_gateway）必须同数
    hdr = {"Authorization": f"Bearer {env['tok']['owner_user']}"}
    for path in (f"/api/keys/{env['key_id']}/usage", f"/api/keys/my/{env['key_id']}/usage"):
        resp = env["client"].get(path, headers=hdr)
        assert resp.status_code == 200, f"{path}: {resp.status_code} {resp.text[:160]}"
        payload = _data(resp)
        assert payload["used_today"] == 1, f"{path} 口径漂移: {payload}"
        assert payload["quota_day"] == "20261103", f"{path} 未标注配额日: {payload}"


def test_管理端今日用量同窗口(env, monkeypatch):
    """admin keys 列表的 used_today 不能再用服务器本地零点。"""
    _clear_usage(env["key_id"])
    monkeypatch.setattr(gw, "_quota_now_utc", lambda: BEIJING_EARLY_MORNING)
    _, day_start = gw.quota_day_start_naive(BEIJING_EARLY_MORNING)
    _add_usage(env["key_id"], at=day_start - timedelta(minutes=1))
    _add_usage(env["key_id"], at=day_start + timedelta(minutes=1))

    r = env["client"].get("/api/admin/skills/keys", headers=_admin_headers(env, "dual"))
    assert r.status_code == 200, r.text[:200]
    keys = {row["id"]: row for row in _data(r)["keys"]}
    assert keys[env["key_id"]]["used_today"] == 1, keys[env["key_id"]]
