"""「免费档」管理端点(2026-09-18 老板需求) —— owner 运行时调整免费级别。

**要解决的事**: 原先"member 能试用哪些功能、每天几次、哪个 skill 免费"全写死在代码里,
改一次要发版。现在它是一份配置(`app_settings.free_tier_config`), owner 在这里直接改,
**30s 内全 worker 热生效**, 不用发版、不用重启。

端点(全部 owner-only):
    GET  /api/admin/free-tier   当前配置 + 可调项目录(功能清单/技能清单/三档限额)
    PUT  /api/admin/free-tier   改配置(局部覆盖, 未给字段沿用当前值)

B3(2026-09-29): 原写死在代码/表里的三组平台限额也收编进这份 KV ——
`max_sessions_per_user`(同时在线设备数, 原 permissions.MAX_SESSIONS_PER_USER=2)、
`guest_strategy`(游客限流, 原 permissions.GUEST_STRATEGY)、
`tier_limits`(skill 档位日限/突发/匀速覆盖, 原 tier_configs 表 + 代码硬编码)。
未配置时一律回退原默认值, 行为与收编前一致。

判定口径: 三条入口(HTTP API / 外部 skill / 聊天工具)统一走 `core.permissions.enforce_perm`
与 `core.free_tier`, 本端点只负责**改配置**, 不另立判定。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.core import free_tier
from src.core.permissions import PRO_ONLY_LABELS, PRO_ONLY_PERMS
from src.web.api.auth import require_owner
from src.web.database import get_db
from src.web.models import User

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-free-tier"])


class FreeTierPatch(BaseModel):
    """局部更新; 只给要改的字段即可。"""

    trial_features: dict[str, str] | list[str] | None = Field(
        default=None,
        description="member 可试用的功能: {权限点: 中文名} 或 [权限点]。空字典/空列表 = 不给任何试用",
    )
    trial_daily_limit: int | None = Field(default=None, ge=0, le=100, description="试用日限(0=不给试用)")
    member_watchlist_max: int | None = Field(default=None, ge=0, le=1000)
    member_alert_max: int | None = Field(default=None, ge=0, le=1000)
    skill_tier_overrides: dict[str, str] | None = Field(
        default=None, description="外部 skill 档位覆盖 {skill名: free|trial|pro}"
    )
    # ── B3(2026-09-29): 原写死在代码/表里的三组平台限额 ──────────────────
    max_sessions_per_user: int | None = Field(
        default=None, ge=1, le=100, description="同账号同时在线设备数上限(默认 2)"
    )
    guest_strategy: dict[str, int] | None = Field(
        default=None,
        description="游客限流策略 {watchlist_limit: 自选上限, get_hourly_limit: 每小时 GET 上限}",
    )
    tier_limits: dict[str, dict[str, int]] | None = Field(
        default=None,
        description="skill 档位限额覆盖 {tier: {daily_limit, burst_limit, refill_per_min}}(空=用表/代码默认)",
    )


def _tier_catalog(db: Session) -> list[dict[str, Any]]:
    """三档当前生效限额(表 + KV 覆盖后的真实值), 供面板对照。"""
    from src.web.api.skills_gateway import (
        TIER_BURST,
        TIER_DAILY_LIMIT,
        TIER_REFILL_PER_MIN,
        refresh_tier_configs,
    )

    refresh_tier_configs(db)
    return [
        {
            "tier": t,
            "daily_limit": TIER_DAILY_LIMIT.get(t),
            "burst_limit": TIER_BURST.get(t),
            "refill_per_min": TIER_REFILL_PER_MIN.get(t),
        }
        for t in ("free", "trial", "pro")
    ]


def _catalog(db: Session) -> dict[str, Any]:
    """可调项目录 —— 让前端不用硬编码功能名/技能名。"""
    from src.web.api.skills_gateway import OPEN_SKILLS

    cfg = free_tier.get_config(db, force=True)
    trials = cfg.get("trial_features") or {}
    skills = []
    for name, meta in sorted(OPEN_SKILLS.items()):
        builtin = str(meta.get("tier_min") or "free")
        skills.append({
            "name": name,
            "builtin_tier": builtin,
            "effective_tier": free_tier.skill_tier_min(name, builtin, db),
            "overridden": name in (cfg.get("skill_tier_overrides") or {}),
        })
    return {
        "features": [
            {
                "perm": perm,
                "label": PRO_ONLY_LABELS.get(perm, perm),
                "trial": perm in trials,
                "trial_label": trials.get(perm),
            }
            for perm in sorted(PRO_ONLY_PERMS)
        ],
        "skills": skills,
        "tiers": _tier_catalog(db),
    }


@router.get("/admin/free-tier")
def get_free_tier(
    db: Session = Depends(get_db),
    _: User = Depends(require_owner),
):
    """当前免费档配置 + 可调项目录。"""
    cfg = free_tier.get_config(db, force=True)
    return {
        "config": cfg,
        "defaults": free_tier._defaults(),  # noqa: SLF001 —— 面板需要展示"默认口径"做对照
        "catalog": _catalog(db),
        "cache_ttl_seconds": int(free_tier._CACHE_TTL),  # noqa: SLF001
    }


@router.put("/admin/free-tier")
def put_free_tier(
    patch: FreeTierPatch,
    db: Session = Depends(get_db),
    user: User = Depends(require_owner),
):
    """改免费档(局部覆盖)。带校验: 只认已知功能点/已知 skill/合法档位。"""
    body = patch.model_dump(exclude_none=True)
    if not body:
        raise HTTPException(400, "没有要修改的字段")

    # ① 试用功能: 只允许 pro 专属功能点(基础功能本来就有, 不该出现在免费档里)
    if "trial_features" in body:
        raw = body["trial_features"]
        if isinstance(raw, list):
            raw = {str(k): PRO_ONLY_LABELS.get(str(k), str(k)) for k in raw}
        unknown = [k for k in raw if k not in PRO_ONLY_PERMS]
        if unknown:
            raise HTTPException(
                400,
                f"未知或非 pro 专属的功能点: {unknown}；可选项: {sorted(PRO_ONLY_PERMS)}",
            )
        body["trial_features"] = {k: str(v) for k, v in raw.items()}

    # ② skill 档位覆盖: 只认已开放的 skill + 合法档位
    if "skill_tier_overrides" in body:
        from src.web.api.skills_gateway import OPEN_SKILLS

        bad = {k: v for k, v in body["skill_tier_overrides"].items()
               if k not in OPEN_SKILLS or v not in free_tier.VALID_TIERS}
        if bad:
            raise HTTPException(
                400,
                f"非法 skill 档位覆盖: {bad}；档位只能是 {list(free_tier.VALID_TIERS)}, "
                "skill 名须在开放清单内",
            )

    # ③ B3: 游客策略: 只认已知子项 + 非负整数
    if "guest_strategy" in body:
        gs = body["guest_strategy"] or {}
        bad_keys = [k for k in gs if k not in free_tier.VALID_GUEST_KEYS]
        bad_vals = {
            k: v for k, v in gs.items()
            if k in free_tier.VALID_GUEST_KEYS
            and (isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100_000)
        }
        if bad_keys or bad_vals:
            raise HTTPException(
                400,
                f"非法游客限流项: keys={bad_keys} values={bad_vals}；"
                f"可调子项 {list(free_tier.VALID_GUEST_KEYS)}(非负整数)",
            )

    # ④ B3: 档位限额: 只认 free/trial/pro + 已知限额项 + 正整数
    if "tier_limits" in body:
        tl = body["tier_limits"] or {}
        bad_tiers = [t for t in tl if t not in free_tier.VALID_TIERS]
        bad_items: dict[str, Any] = {}
        for t, lim in tl.items():
            if t not in free_tier.VALID_TIERS or not isinstance(lim, dict):
                continue
            for k, v in lim.items():
                if k not in free_tier.VALID_TIER_LIMIT_KEYS or isinstance(v, bool) \
                        or not isinstance(v, int) or v <= 0:
                    bad_items.setdefault(t, {})[k] = v
        if bad_tiers or bad_items:
            raise HTTPException(
                400,
                f"非法档位限额: tiers={bad_tiers} items={bad_items}；"
                f"档位只能是 {list(free_tier.VALID_TIERS)}, 限额项 "
                f"{list(free_tier.VALID_TIER_LIMIT_KEYS)}(正整数)",
            )

    cfg = free_tier.save_config(db, body, actor=getattr(user, "username", "") or "")
    try:
        from src.web.api.audit import log_audit

        log_audit(db, user, "free_tier_update", detail=str(body)[:500])
    except Exception as exc:  # noqa: BLE001 —— 审计失败不影响配置已生效
        logger.debug("free_tier 审计落库失败: %r", exc)
    return {"config": cfg, "catalog": _catalog(db)}
