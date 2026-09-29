# -*- coding: utf-8 -*-
"""B3(2026-09-29): 平台限额收编进「免费档」KV 的回归。

背景: 三组限额原先散落在代码/表里 ——
  - `MAX_SESSIONS_PER_USER`(同时在线设备数, permissions.py 写死 2)
  - `GUEST_STRATEGY`(游客限流: 自选上限 / 每小时 GET 上限)
  - `tier_configs`(skill 档位日限/突发/匀速, 表 + skills_gateway 硬编码兜底)
现在统一进 `app_settings.free_tier_config`(同一份 KV + 30s 缓存), owner 在
`GET/PUT /api/admin/free-tier` 面板可调, 改完 30s 内热生效, **不用发版**。

本文件钉四件事:
  ① 未配置 → 一律回退原硬编码默认值(行为与收编前完全一致);
  ② 配置后 → 真的生效, 且写后缓存立即失效(同一进程内热更新);
  ③ 非法值(负数/非数字/越界/未知项) → 安全回退默认或 400 拒绝, 不 500;
  ④ 多用户/多档位互不串味。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.core import demo_limit
from src.core import free_tier
from src.core import permissions as perm


# ── 夹具 ────────────────────────────────────────────────────────────


@pytest.fixture()
def db():
    from src.web.database import SessionLocal

    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _wipe(db):
    from src.db.models import AppSettings

    db.query(AppSettings).filter(AppSettings.key == free_tier.CONFIG_KEY).delete()
    db.commit()
    free_tier.invalidate_cache()


@pytest.fixture(autouse=True)
def _reset(db):
    """用例前后都回默认口径; 档位全局量(skills_gateway)也要还原, 免串味。"""
    import src.web.api.skills_gateway as gw

    saved = (
        dict(gw.TIER_DAILY_LIMIT),
        dict(gw.TIER_BURST),
        dict(gw.TIER_REFILL_PER_MIN),
    )
    _wipe(db)
    yield
    _wipe(db)
    gw.TIER_DAILY_LIMIT.clear()
    gw.TIER_DAILY_LIMIT.update(saved[0])
    gw.TIER_BURST.clear()
    gw.TIER_BURST.update(saved[1])
    gw.TIER_REFILL_PER_MIN.clear()
    gw.TIER_REFILL_PER_MIN.update(saved[2])
    gw._tier_cfg_loaded_at = 0.0  # 下一个用例重新从表/配置读


def _u(role: str = "member", uid: str = "u-plimit"):
    return SimpleNamespace(id=uid, role=role, username=f"{role}-{uid}")


def _sessions(db, user_id: str, n: int, *, prefix: str = "s") -> None:
    """给某用户塞 n 条未过期会话(设备限制用)。"""
    from src.db.models import UserSession

    db.query(UserSession).filter(UserSession.user_id == user_id).delete()
    db.commit()
    now = datetime.now(timezone.utc)
    for i in range(n):
        db.add(UserSession(
            session_id=f"{prefix}-{user_id}-{i}",
            user_id=user_id,
            expires_at=now + timedelta(hours=1),
            last_seen=now + timedelta(seconds=i),  # 越大越新
        ))
    db.commit()


def _alive(db, user_id: str) -> int:
    from src.db.models import UserSession

    return db.query(UserSession).filter(
        UserSession.user_id == user_id,
        UserSession.expires_at > datetime.now(timezone.utc),
    ).count()


# ── ① 未配置 → 回退原默认 ───────────────────────────────────────────


def test_defaults_when_unconfigured(db):
    cfg = free_tier.get_config(db, force=True)
    assert cfg["max_sessions_per_user"] == 2 == perm.MAX_SESSIONS_PER_USER
    assert cfg["guest_strategy"] == perm.GUEST_STRATEGY == {
        "watchlist_limit": 1, "get_hourly_limit": 20,
    }
    assert cfg["tier_limits"] == {}  # 空 = 用 tier_configs 表 / 代码兜底

    assert free_tier.max_sessions_per_user(db) == 2
    assert perm.effective_max_sessions(db) == 2
    assert free_tier.guest_strategy(db) == dict(perm.GUEST_STRATEGY)
    assert perm.effective_guest_strategy(db) == dict(perm.GUEST_STRATEGY)
    # 无 db 上下文也拿默认(缓存为空), 不报错
    assert free_tier.max_sessions_per_user() == 2
    assert free_tier.guest_strategy() == {"watchlist_limit": 1, "get_hourly_limit": 20}


def test_default_hourly_limit_is_20(db):
    assert demo_limit.get_hourly_limit(db) == 20 == demo_limit._DEMO_GET_HOURLY_LIMIT
    assert demo_limit.get_hourly_limit(None) == 20
    uid = "u-demo-default"
    demo_limit._get_state.pop(uid, None)
    allowed = sum(1 for _ in range(25) if demo_limit.allow_api_get(uid, db))
    demo_limit._get_state.pop(uid, None)
    assert allowed == 20  # 第 21 次起超限


def test_default_tier_limits_unchanged_by_refresh(db):
    """未配置 tier_limits → refresh_tier_configs 后与 tier_configs 表默认一致(100/500/5000)。"""
    import src.web.api.skills_gateway as gw

    gw._tier_cfg_loaded_at = 0.0
    gw.refresh_tier_configs(db)
    assert gw.TIER_DAILY_LIMIT["free"] == 100
    assert gw.TIER_DAILY_LIMIT["trial"] == 500
    assert gw.TIER_DAILY_LIMIT["pro"] == 5000
    assert gw.TIER_BURST == {"free": 30, "trial": 50, "pro": 100}
    assert gw.TIER_REFILL_PER_MIN == {"free": 15, "trial": 20, "pro": 60}


# ── ② 配置后生效 + 缓存内热更新 ─────────────────────────────────────


def test_device_limit_hot_applies(db):
    u = _u("member", "u-device")
    _sessions(db, u.id, 3, prefix="a")
    assert _alive(db, u.id) == 3

    # 默认上限 2: 3 条已过期会话里最新的一条是本次登录 → 踢最早的 1 条, 保留 == 上限
    perm.enforce_device_limit(db, u, "a-u-device-3")
    assert _alive(db, u.id) == 2  # 3 条 > 上限 2 → 踢掉最早的 1 条

    # 调大到 4 → 写后缓存立即失效(同一进程热生效)
    free_tier.save_config(db, {"max_sessions_per_user": 4}, actor="test")
    assert perm.effective_max_sessions(db) == 4
    _sessions(db, u.id, 3, prefix="b")
    perm.enforce_device_limit(db, u, "b-u-device-2")
    assert _alive(db, u.id) == 3  # 上限 4 → 一条都不踢

    # 调小到 1 → 一次踢到「未过期会话 == 上限」
    free_tier.save_config(db, {"max_sessions_per_user": 1}, actor="test")
    perm.enforce_device_limit(db, u, "b-u-device-2")
    assert _alive(db, u.id) == 1


def test_guest_hourly_limit_hot_applies(db):
    free_tier.save_config(db, {"guest_strategy": {"get_hourly_limit": 3}}, actor="test")
    assert demo_limit.get_hourly_limit(db) == 3

    uid = "u-demo-cfg"
    demo_limit._get_state.pop(uid, None)
    allowed = sum(1 for _ in range(6) if demo_limit.allow_api_get(uid, db))
    demo_limit._get_state.pop(uid, None)
    assert allowed == 3

    # 未配置的子项保持默认(局部覆盖语义)
    assert free_tier.guest_strategy(db)["watchlist_limit"] == 1


def test_guest_watchlist_limit_accessor(db):
    free_tier.save_config(db, {"guest_strategy": {"watchlist_limit": 3}}, actor="test")
    assert perm.effective_guest_strategy(db)["watchlist_limit"] == 3


def test_tier_limits_override_wins_over_table(db):
    import src.web.api.skills_gateway as gw

    free_tier.save_config(
        db,
        {"tier_limits": {"free": {"daily_limit": 7, "burst_limit": 2, "refill_per_min": 1}}},
        actor="test",
    )
    gw._tier_cfg_loaded_at = 0.0
    gw.refresh_tier_configs(db)
    assert gw.TIER_DAILY_LIMIT["free"] == 7
    assert gw.TIER_BURST["free"] == 2
    assert gw.TIER_REFILL_PER_MIN["free"] == 1
    # 未覆盖的档位不受影响(表默认)
    assert gw.TIER_DAILY_LIMIT["pro"] == 5000


# ── ③ 非法值 → 安全回退 / 400 ───────────────────────────────────────


def test_invalid_values_fall_back_to_defaults(db):
    # 负数 / 非数字 / 越界: _coerce 丢弃回默认, 不抛错
    free_tier.save_config(
        db,
        {
            "max_sessions_per_user": -5,
            "guest_strategy": {"watchlist_limit": "abc", "get_hourly_limit": -1},
            "tier_limits": {"free": {"daily_limit": -3}, "nope": {"daily_limit": 10}},
        },
        actor="test",
    )
    cfg = free_tier.get_config(db, force=True)
    assert cfg["max_sessions_per_user"] == 2
    assert cfg["guest_strategy"] == {"watchlist_limit": 1, "get_hourly_limit": 20}
    assert cfg["tier_limits"] == {}
    assert free_tier.max_sessions_per_user(db) == 2
    assert demo_limit.get_hourly_limit(db) == 20


def test_bool_and_huge_values_rejected(db):
    free_tier.save_config(
        db,
        {"max_sessions_per_user": True, "guest_strategy": {"get_hourly_limit": 10_000_000}},
        actor="test",
    )
    cfg = free_tier.get_config(db, force=True)
    assert cfg["max_sessions_per_user"] == 2  # bool 不算整数
    assert cfg["guest_strategy"]["get_hourly_limit"] == 20  # 越界丢弃


def test_cached_config_and_no_db_hourly_limit(db):
    """写配置后 L1 缓存立即失效; 读一次后缓存命中, 无 db 的调用方也能拿到新值。"""
    free_tier.save_config(db, {"guest_strategy": {"get_hourly_limit": 5}}, actor="test")
    assert free_tier.cached_config() is None  # 写后立即失效(不是等 30s)

    free_tier.get_config(db, force=True)  # 触发一次带 db 的读取 → 填充缓存
    assert free_tier.cached_config()["guest_strategy"]["get_hourly_limit"] == 5
    assert demo_limit.get_hourly_limit() == 5  # 无 db 走缓存, 不打库


def test_admin_put_rejects_illegal_platform_limits(client):
    from src.web.app import app

    app.dependency_overrides.clear()
    _as(app, _u("owner", "u-http-plimit"))
    H = {"Authorization": "Bearer test-token-for-csrf-skip"}

    # max_sessions_per_user 越界 → 422(pydantic ge/le)
    assert client.put(
        "/api/admin/free-tier", json={"max_sessions_per_user": 0}, headers=H
    ).status_code == 422

    # 未知游客子项 / 负值 → 400
    assert client.put(
        "/api/admin/free-tier", json={"guest_strategy": {"nope": 1}}, headers=H
    ).status_code == 400
    assert client.put(
        "/api/admin/free-tier", json={"guest_strategy": {"get_hourly_limit": -1}}, headers=H
    ).status_code == 400

    # 未知档位 / 非正限额 → 400
    assert client.put(
        "/api/admin/free-tier", json={"tier_limits": {"god": {"daily_limit": 1}}}, headers=H
    ).status_code == 400
    assert client.put(
        "/api/admin/free-tier", json={"tier_limits": {"free": {"daily_limit": 0}}}, headers=H
    ).status_code == 400

    # 合法 → 200 且落库
    ok = client.put(
        "/api/admin/free-tier",
        json={
            "max_sessions_per_user": 3,
            "guest_strategy": {"watchlist_limit": 2, "get_hourly_limit": 30},
            "tier_limits": {"pro": {"daily_limit": 6000}},
        },
        headers=H,
    )
    assert ok.status_code == 200, ok.text
    data = ok.json().get("data", ok.json())
    assert data["config"]["max_sessions_per_user"] == 3
    assert data["config"]["guest_strategy"] == {"watchlist_limit": 2, "get_hourly_limit": 30}
    assert data["config"]["tier_limits"] == {"pro": {"daily_limit": 6000}}
    # 目录带三档当前生效值(面板不用硬编码); 档位限额本身有 30s TTL, 这里显式过期再读
    import src.web.api.skills_gateway as gw

    gw._tier_cfg_loaded_at = 0.0
    r2 = client.get("/api/admin/free-tier")
    assert r2.status_code == 200, r2.text
    d2 = r2.json().get("data", r2.json())
    tiers = {t["tier"]: t for t in d2["catalog"]["tiers"]}
    assert tiers["pro"]["daily_limit"] == 6000  # 配置覆盖优先于 tier_configs 表
    assert tiers["free"]["daily_limit"] == 100  # 未覆盖档位仍是表默认


def test_admin_get_exposes_platform_limits(client):
    from src.web.app import app

    app.dependency_overrides.clear()
    _as(app, _u("owner", "u-http-plimit-get"))
    r = client.get("/api/admin/free-tier")
    assert r.status_code == 200, r.text
    data = r.json().get("data", r.json())
    assert data["config"]["max_sessions_per_user"] == 2
    assert data["config"]["guest_strategy"] == {"watchlist_limit": 1, "get_hourly_limit": 20}
    assert data["defaults"]["max_sessions_per_user"] == 2
    assert {t["tier"] for t in data["catalog"]["tiers"]} == {"free", "trial", "pro"}


# ── ④ 多用户 / 多档位不串味 ─────────────────────────────────────────


def test_device_limit_is_per_user(db):
    a = _u("member", "u-multi-a")
    b = _u("member", "u-multi-b")
    _sessions(db, a.id, 3, prefix="a")
    _sessions(db, b.id, 3, prefix="b")

    perm.enforce_device_limit(db, a, "a-u-multi-a-2")

    assert _alive(db, a.id) == 2   # A 被踢 1
    assert _alive(db, b.id) == 3   # B 完全不受影响


def test_tier_limits_per_tier(db):
    import src.web.api.skills_gateway as gw

    free_tier.save_config(
        db,
        {"tier_limits": {"free": {"daily_limit": 11}, "pro": {"daily_limit": 22}}},
        actor="test",
    )
    gw._tier_cfg_loaded_at = 0.0
    gw.refresh_tier_configs(db)
    assert gw.TIER_DAILY_LIMIT["free"] == 11
    assert gw.TIER_DAILY_LIMIT["pro"] == 22
    assert gw.TIER_DAILY_LIMIT["trial"] == 500  # 未配置的档位不动


def test_config_is_shared_kv_not_per_user(db):
    """配置是全局一份(owner 改全站生效), 不是 per-user 覆盖 —— 别把多用户隔离做错方向。"""
    from src.db.models import AppSettings

    free_tier.save_config(db, {"max_sessions_per_user": 7}, actor="owner-a")
    rows = db.query(AppSettings).filter(AppSettings.key == free_tier.CONFIG_KEY).all()
    assert len(rows) == 1
    assert free_tier.max_sessions_per_user(db) == 7  # 任何调用方(任何 user)都读到 7


# ── HTTP 客户端夹具(放最后: 与上面 fixture 复用) ─────────────────────


@pytest.fixture()
def client(db):
    from src.web.app import app

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _as(app, user):
    from src.web.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user
