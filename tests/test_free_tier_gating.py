# -*- coding: utf-8 -*-
"""免费档(免费级别)可调 + 数智决策/集合竞价池 pro 收口 回归(2026-09-18)。

老板口径:
- **数智决策三指标**(机构活跃度 + GS + L2主力净流入 TQ口径, 权限点 `view_forecast`)与
  **集合竞价池**(9:25 竞价数据, `view_auction`)**一律 pro 档, 不在免费层级** ——
  HTTP API / 外部 skill / 聊天工具三条入口都一样。
- owner 要能**运行时**调整"免费到哪一级": 哪些功能可试用、日限多少、哪个 skill 免费。

本文件钉五件事:
  ① 默认 member 拿不到这两类功能(403 + pro 引导, 不是"沉默失败");
  ② 把功能加进免费档后 member 能用, 且**日限真的生效**(用完即 403);
  ③ 配置读写往返 + 脏数据回落默认(配置读坏不能让全站 500);
  ④ skill 侧默认 pro, 且 owner 的覆盖能把它调回 free(游客/免费 key 同步生效);
  ⑤ /api/admin/free-tier 是 owner 专属, 且带校验(未知功能点/非法 skill 档位拒绝)。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

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


@pytest.fixture(autouse=True)
def _reset_free_tier(db):
    """每个用例前后都回到默认口径, 免得上个用例改的配置串味。"""
    from src.db.models import AppSettings

    def _wipe():
        db.query(AppSettings).filter(AppSettings.key == free_tier.CONFIG_KEY).delete()
        db.commit()
        free_tier.invalidate_cache()

    _wipe()
    yield
    _wipe()


def _u(role: str, uid: str = "u-free-tier"):
    from types import SimpleNamespace

    return SimpleNamespace(id=uid, role=role, username=f"{role}-{uid}")


# ── ① 默认: 两类功能都是 pro 专属 ───────────────────────────────────


@pytest.mark.parametrize("role", ["member", "guest"])
@pytest.mark.parametrize("p", [perm.PERM_VIEW_FORECAST, perm.PERM_VIEW_AUCTION])
def test_default_denies_non_pro_with_upgrade_guide(role, p):
    with pytest.raises(HTTPException) as ei:
        perm.enforce_perm(_u(role), p, None)
    assert ei.value.status_code == 403
    detail = ei.value.detail
    assert isinstance(detail, dict)
    assert detail.get("pro_only") is True
    assert detail.get("pro_guide") is True
    assert "Pro" in detail.get("message", "")


@pytest.mark.parametrize("role", ["pro", "owner"])
@pytest.mark.parametrize("p", [perm.PERM_VIEW_FORECAST, perm.PERM_VIEW_AUCTION])
def test_default_allows_pro_and_owner(role, p):
    assert perm.enforce_perm(_u(role), p, None) is None


def test_member_base_no_longer_contains_pro_only_perms():
    """重构后 member 基础权限**不含** pro 专属功能(旧实现里 view_forecast 在基础权限内)。"""
    base = perm.ROLE_PERMISSIONS[perm.ROLE_MEMBER]
    for p in perm.PRO_ONLY_PERMS:
        assert p not in base, f"{p} 不应出现在 member 基础权限里"
    assert perm.PERM_VIEW_QUOTE in base  # 行情等基础能力不受影响
    pro = perm.ROLE_PERMISSIONS[perm.ROLE_PRO]
    for p in perm.PRO_ONLY_PERMS:
        assert p in pro


def test_default_trial_features_exclude_forecast_and_auction(db):
    """默认免费档只剩 机会/L2/暗盘 —— 数智决策与集合竞价池不在里面。"""
    trials = free_tier.trial_features(db)
    assert "view_forecast" not in trials
    assert "view_auction" not in trials
    assert set(trials) == {"view_opportunities", "view_l2", "view_dark"}
    assert free_tier.trial_daily_limit(db) == 3


# ── ② 免费档可调 + 日限真的生效 ─────────────────────────────────────


def test_owner_can_grant_trial_and_daily_limit_applies(db):
    free_tier.save_config(
        db,
        {"trial_features": {"view_auction": "集合竞价池"}, "trial_daily_limit": 1},
        actor="owner-test",
    )
    u = _u("member", "u-trial-limit")

    # 第 1 次: 放行(试用计数 +1)
    assert perm.enforce_perm(u, perm.PERM_VIEW_AUCTION, db) is None
    assert perm.get_trial_remaining(db, u.id, perm.PERM_VIEW_AUCTION) == 0

    # 第 2 次: 日限已到 → 403(且提示的是"试用次数用完", 而不是 pro_only)
    with pytest.raises(HTTPException) as ei:
        perm.enforce_perm(u, perm.PERM_VIEW_AUCTION, db)
    assert ei.value.status_code == 403
    assert ei.value.detail.get("pro_guide") is True
    assert "试用次数已用完" in ei.value.detail.get("message", "")
    assert ei.value.detail.get("pro_only") is None


def test_daily_limit_zero_means_no_trial_at_all(db):
    free_tier.save_config(
        db, {"trial_features": {"view_l2": "L2资金"}, "trial_daily_limit": 0}, actor="owner-test"
    )
    with pytest.raises(HTTPException) as ei:
        perm.enforce_perm(_u("member", "u-zero"), perm.PERM_VIEW_L2, db)
    assert ei.value.status_code == 403
    assert "试用次数已用完" in ei.value.detail.get("message", "")


def test_revoke_trial_returns_to_pro_only(db):
    """先给试用, 再撤掉 → 立刻回到 pro_only 拒绝(热生效, 不用发版)。"""
    free_tier.save_config(db, {"trial_features": {"view_forecast": "数智决策"}}, actor="t")
    assert perm.enforce_perm(_u("member", "u-revoke"), perm.PERM_VIEW_FORECAST, db) is None

    free_tier.save_config(db, {"trial_features": {}}, actor="t")
    with pytest.raises(HTTPException) as ei:
        perm.enforce_perm(_u("member", "u-revoke"), perm.PERM_VIEW_FORECAST, db)
    assert ei.value.detail.get("pro_only") is True


def test_quota_limits_are_configurable(db):
    free_tier.save_config(db, {"member_watchlist_max": 1, "member_alert_max": 0}, actor="t")
    assert free_tier.member_watchlist_max(db) == 1
    assert free_tier.member_alert_max(db) == 0


# ── ③ 配置读写往返 + 脏数据回落 ─────────────────────────────────────


def test_config_roundtrip_and_partial_patch(db):
    free_tier.save_config(db, {"trial_daily_limit": 7}, actor="t")
    cfg = free_tier.get_config(db, force=True)
    assert cfg["trial_daily_limit"] == 7
    # 局部覆盖: 只给 skill 覆盖, 日限保持 7(不被重置回默认 3)
    free_tier.save_config(db, {"skill_tier_overrides": {"get_auction_data": "free"}}, actor="t")
    cfg = free_tier.get_config(db, force=True)
    assert cfg["trial_daily_limit"] == 7
    assert cfg["skill_tier_overrides"] == {"get_auction_data": "free"}


def test_dirty_config_falls_back_to_defaults(db):
    from src.db.models import AppSettings

    row = db.query(AppSettings).filter(AppSettings.key == free_tier.CONFIG_KEY).first()
    raw = "{not-json"
    if row is None:
        db.add(AppSettings(key=free_tier.CONFIG_KEY, value=raw, description="坏配置"))
    else:
        row.value = raw
    db.commit()
    free_tier.invalidate_cache()
    cfg = free_tier.get_config(db, force=True)
    assert cfg["trial_daily_limit"] == free_tier.DEFAULT_TRIAL_DAILY_LIMIT
    assert set(cfg["trial_features"]) == set(free_tier.DEFAULT_TRIAL_FEATURES)

    # 非法值(档位不在白名单 / 越界日限)同样被剔除, 不留脏值
    from src.core.free_tier import _coerce

    bad = _coerce({"trial_daily_limit": 9999, "skill_tier_overrides": {"x": "god"}})
    assert bad["trial_daily_limit"] == free_tier.DEFAULT_TRIAL_DAILY_LIMIT
    assert bad["skill_tier_overrides"] == {}


def test_config_cache_returns_copy(db):
    """缓存返回深拷贝 —— 调用方改返回值不能污染后续判定。"""
    cfg = free_tier.get_config(db)
    cfg["trial_daily_limit"] = 999
    assert free_tier.get_config(db)["trial_daily_limit"] != 999


# ── ④ skill 侧档位 ──────────────────────────────────────────────────


def test_skill_defaults_are_pro_for_auction_and_decision_pioneer():
    from src.web.api.skills_gateway import OPEN_SKILLS

    for name in ("get_auction_data", "get_decision_pioneer"):
        assert OPEN_SKILLS[name]["tier_min"] == "pro", f"{name} 默认应为 pro 档"


def test_skill_guest_blocked_for_pro_default():
    from src.web.api.skills_gateway import OPEN_SKILLS, TIER_RANK

    # 游客只放行 tier_min=free; 这两支现在不是 free → 游客拿不到
    for name in ("get_auction_data", "get_decision_pioneer"):
        assert OPEN_SKILLS[name]["tier_min"] != "free"
    assert TIER_RANK["free"] < TIER_RANK["pro"]


def test_skill_tier_override_from_config(db):
    from src.web.api.skills_gateway import OPEN_SKILLS, _effective_tier_min

    meta = OPEN_SKILLS["get_auction_data"]
    assert _effective_tier_min("get_auction_data", meta, db) == "pro"  # 默认

    free_tier.save_config(db, {"skill_tier_overrides": {"get_auction_data": "free"}}, actor="t")
    assert _effective_tier_min("get_auction_data", meta, db) == "free"  # 覆盖生效

    # 未覆盖的 skill 不受影响
    other = OPEN_SKILLS["get_stock_quote"]
    assert _effective_tier_min("get_stock_quote", other, db) == "free"


# ── ⑤ HTTP 层: 路由收口 + admin 端点 ────────────────────────────────


@pytest.fixture()
def client(db):
    from src.web.api.auth import get_current_user
    from src.web.app import app

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _as(app, user):
    from src.web.api.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user


def test_resonance_endpoint_403_for_member(client):
    """三指标扫描接口对 member 403(不是 500/不是空数据)。"""
    from src.web.app import app

    _as(app, _u("member", "u-http-member"))
    r = client.get("/api/resonance/scan")
    assert r.status_code == 403, r.text
    body = r.json()
    detail = body.get("detail") or body.get("message") or body
    assert "Pro" in str(detail) or "pro" in str(detail)


def test_auction_pool_endpoint_403_for_member(client):
    from src.web.app import app

    _as(app, _u("member", "u-http-member2"))
    r = client.get("/api/auction/anomaly")
    assert r.status_code == 403, r.text


def test_auction_endpoints_ok_for_owner(client):
    """owner 不被权限拦(端点自身的数据可用性另说 —— 这里只断言"不是权限 403")。"""
    from src.web.app import app

    _as(app, _u("owner", "u-http-owner"))
    r = client.get("/api/auction/anomaly")
    assert r.status_code != 403, r.text
    r2 = client.get("/api/auction-snapshot?symbol=002361")
    assert r2.status_code != 403, r2.text


def test_admin_free_tier_owner_only(client):
    from src.web.app import app

    _as(app, _u("member", "u-http-member3"))
    assert client.get("/api/admin/free-tier").status_code == 403

    _as(app, _u("owner", "u-http-owner2"))
    r = client.get("/api/admin/free-tier")
    assert r.status_code == 200, r.text
    data = r.json().get("data", r.json())
    assert data["config"]["trial_daily_limit"] == free_tier.DEFAULT_TRIAL_DAILY_LIMIT
    perms = {f["perm"] for f in data["catalog"]["features"]}
    assert "view_forecast" in perms and "view_auction" in perms


def test_admin_free_tier_put_validates_and_persists(client):
    from src.web.app import app

    _as(app, _u("owner", "u-http-owner3"))

    # 写请求带 `Authorization: Bearer`(前端真实路径就是 localStorage token) ——
    # CSRF 中间件对 Bearer 天然免验(浏览器跨站不会自动带该头), 这里与被测行为一致。
    H = {"Authorization": "Bearer test-token-for-csrf-skip"}

    # 未知功能点 → 400
    bad = client.put("/api/admin/free-tier", json={"trial_features": {"view_nope": "X"}}, headers=H)
    assert bad.status_code == 400, bad.text

    # 非法 skill 档位 → 400
    bad2 = client.put(
        "/api/admin/free-tier",
        json={"skill_tier_overrides": {"get_auction_data": "god"}},
        headers=H,
    )
    assert bad2.status_code == 400, bad2.text

    # 合法更新 → 200 且落库
    ok = client.put(
        "/api/admin/free-tier",
        json={"trial_features": ["view_auction"], "trial_daily_limit": 5},
        headers=H,
    )
    assert ok.status_code == 200, ok.text
    data = ok.json().get("data", ok.json())
    assert data["config"]["trial_daily_limit"] == 5
    assert "view_auction" in data["config"]["trial_features"]
    # 目录里该功能的 trial 标记同步变 true
    feat = {f["perm"]: f for f in data["catalog"]["features"]}
    assert feat["view_auction"]["trial"] is True
