"""Pro 付费系统(2026-09-16 任务3.1/3.3): 申请 + 人工审核 + 到期降级查询。

路由(挂 /api 前缀):
- POST /api/pro/apply                 用户提交 Pro 申请(需登录)
- GET  /api/pro/apply/status          用户查自己的申请状态
- GET  /api/pro/admin/applications    owner 查看所有申请(可按 status 过滤)
- POST /api/pro/admin/approve         owner 批准: users.role→pro + key tier→pro
- POST /api/pro/admin/reject          owner 拒绝
- GET  /api/pro/admin/expired         owner 查看已降级的 key(tier_downgrade_logs)

红线:
- admin 接口一律 require_owner
- 批准/拒绝只允许 pending 状态的申请(防重复审)
- 档位日限从 tier_configs 热更新读取(skills_gateway.refresh_tier_configs)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.db.models import (
    ProApplication,
    SkillApiKey,
    TierDowngradeLog,
    User,
)
from src.web.api.auth import get_current_user, require_owner
from src.web.api._scope import allow_cross_user  # C3(2026-09-18): owner-only 端点的显式跨用户豁免标记
from src.web.api.skills_gateway import (
    TIER_DAILY_LIMIT,
    ensure_downgrade_scheduler,
    refresh_tier_configs,
)
from src.web.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["pro-billing"])

# 模块导入即启动到期降级后台线程(幂等; daemon, 进程退出自动停)
try:
    ensure_downgrade_scheduler()
except Exception as e:  # noqa: BLE001
    logger.warning("启动到期降级调度失败(不影响请求路径): %r", e)


# ── 请求模型 ────────────────────────────────────────────────────────

class ApplyRequest(BaseModel):
    reason: str = Field(default="", max_length=500, description="申请理由")
    contact: str = Field(default="", max_length=64, description="联系方式(可选)")


class ReviewRequest(BaseModel):
    application_id: int = Field(..., description="申请 ID")
    note: str = Field(default="", max_length=255, description="审核备注")


# ── 工具 ────────────────────────────────────────────────────────────

def _app_to_dict(a: ProApplication) -> dict[str, Any]:
    return {
        "id": a.id,
        "user_id": a.user_id,
        "username": a.username,
        "reason": a.reason or "",
        "status": a.status,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None,
        "reviewed_by": a.reviewed_by or "",
        "review_note": a.review_note or "",
    }


def _upgrade_user_keys_to_pro(db: Session, user_id: str) -> int:
    """把该用户名下全部 active key 升为 pro 档(日限取 tier_configs)。返回处理数量。"""
    refresh_tier_configs(db)
    pro_limit = TIER_DAILY_LIMIT["pro"]
    keys = (
        db.query(SkillApiKey)
        .filter(
            SkillApiKey.user_id == user_id,
            SkillApiKey.status == "active",
        )
        .all()
    )
    n = 0
    for k in keys:
        k.tier = "pro"
        k.daily_limit = pro_limit
        # 人工批准的 pro 不设自动到期(续费/降级由 owner 手动或到期任务处理)
        k.expires_at = None
        n += 1
    db.commit()
    return n


# ── 公开档位对比(P2-4, 2026-09-18) ────────────────────────────────────

#: 档位展示顺序与中文名(goest→owner; 权限点来自 permissions.ROLE_PERMISSIONS, 不在这里重复定义)
#: **只列用户可选档位** —— guest(体验账号)权限集是空的(`ROLE_PERMISSIONS[guest]=frozenset()`),
#: 摆一列空的会让人以为页面坏了; owner 是管理角色, 不是可购档位。体验账号的受限说明走 `demo_note`。
_TIER_ORDER = (("member", "免费"), ("pro", "Pro"))


@router.get("/tiers")
def list_tiers(db: Session = Depends(get_db)) -> dict:
    """公开档位对比(**无需登录**; 落地页/档位页用)。

    **为什么由后端生成**: 权限点与免费档上限都是运行时可调的(`free_tier` 存 app_settings),
    前端硬编码一份必然漂移 —— 这里直接从 `permissions.get_role_permissions` + `PERMISSION_LABELS`
    + `free_tier` 现读现拼, 前端只负责排版。

    **诚实口径**: 内测期**不收费**, 所以不返回任何价格字段(只返回 `billing_enabled=false`);
    Pro 走"申请 → 管理员开通"; 免费档上限是**运行时真值**(不是文档里的旧数字)。
    """
    from src.core import free_tier
    from src.core.permissions import PERMISSION_LABELS, get_role_permissions

    rows: list[dict] = []
    for role, label in _TIER_ORDER:
        perms = get_role_permissions(role)
        groups: dict[str, list[str]] = {}
        for key in sorted(perms, key=lambda k: (PERMISSION_LABELS.get(k, ("", ""))[1], k)):
            name, group = PERMISSION_LABELS.get(key, (key, "其他"))
            groups.setdefault(group, []).append(name)
        rows.append({
            "key": role,
            "label": label,
            "groups": [{"group": g, "items": items} for g, items in sorted(groups.items())],
            "count": len(perms),
        })

    # 免费档的运行时可调上限(现读, 30s 缓存由 free_tier 内部管)
    try:
        limits = {
            "watchlist_max": free_tier.member_watchlist_max(db),
            "alert_max": free_tier.member_alert_max(db),
            "trial_daily_limit": free_tier.trial_daily_limit(db),
        }
    except Exception:  # noqa: BLE001  —— 读不到就如实说"暂不可读", 不编数字
        limits = None

    return {
        "tiers": rows,
        "member_limits": limits,
        "billing_enabled": False,
        "note": "内测期不收费。Pro 需提交申请, 由管理员开通。",
        "apply_endpoint": "/api/pro/apply",
        "limits_note": "免费档上限由管理员在设置页调整, 此处为实时值。" if limits else "免费档上限暂时读不到(数据库不可用)。",
        "demo_note": "另有时限体验账号(受限较多), 由管理员按需开通。",
    }


# ── 用户侧 ──────────────────────────────────────────────────────────

@router.post("/pro/apply")
def apply_pro(
    body: ApplyRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """提交 Pro 申请。同一用户仅允许一条 pending。"""
    if user.role == "owner":
        raise HTTPException(400, "owner 无需申请 Pro")
    if user.role == "pro":
        raise HTTPException(400, "当前账号已是 Pro")

    existing = (
        db.query(ProApplication)
        .filter(
            ProApplication.user_id == str(user.id),
            ProApplication.status == "pending",
        )
        .first()
    )
    if existing:
        raise HTTPException(400, "已有待审核的 Pro 申请, 请耐心等待")

    reason = (body.reason or "").strip()
    if body.contact:
        reason = f"{reason}\n[contact] {body.contact.strip()}" if reason else f"[contact] {body.contact.strip()}"

    row = ProApplication(
        user_id=str(user.id),
        username=user.username or "",
        reason=reason[:2000],
        status="pending",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("Pro 申请提交 user=%s id=%s", user.username, row.id)
    try:
        from src.web.api.audit import log_audit

        log_audit(db, user, "pro_apply", detail=f"提交 Pro 申请 id={row.id}")
    except Exception:  # noqa: BLE001
        pass
    return {
        "status": "pending",
        "application": _app_to_dict(row),
        "message": "申请已提交, 请等待人工审核。",
    }


@router.get("/pro/apply/status")
def apply_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """查自己最近一条 Pro 申请状态。"""
    row = (
        db.query(ProApplication)
        .filter(ProApplication.user_id == str(user.id))
        .order_by(ProApplication.created_at.desc(), ProApplication.id.desc())
        .first()
    )
    return {
        "role": user.role,
        "application": _app_to_dict(row) if row else None,
        "has_application": row is not None,
    }


# ── admin(owner only) ──────────────────────────────────────────────

@router.get("/pro/admin/applications")
@allow_cross_user  # C3(2026-09-18): owner-only(Depends(require_owner)) 的申请审核列表, 有意跨用户
def admin_list_applications(
    status: str = Query(default="", description="pending/approved/rejected, 空=全部"),
    limit: int = Query(default=100, ge=1, le=500),
    _owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """申请列表(owner)。默认按创建时间倒序。"""
    q = db.query(ProApplication)
    s = (status or "").strip().lower()
    if s:
        if s not in ("pending", "approved", "rejected"):
            raise HTTPException(400, "status 必须是 pending/approved/rejected")
        q = q.filter(ProApplication.status == s)
    rows = (
        q.order_by(ProApplication.created_at.desc(), ProApplication.id.desc())
        .limit(limit)
        .all()
    )
    pending_n = db.query(ProApplication).filter(ProApplication.status == "pending").count()
    return {
        "applications": [_app_to_dict(r) for r in rows],
        "pending_count": pending_n,
    }


@router.post("/pro/admin/approve")
def admin_approve(
    body: ReviewRequest,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """批准 Pro 申请: role→pro + 该用户 active key tier→pro。"""
    app_row = db.query(ProApplication).filter(ProApplication.id == body.application_id).first()
    if not app_row:
        raise HTTPException(404, "申请不存在")
    if app_row.status != "pending":
        raise HTTPException(400, f"申请已处理过(status={app_row.status}), 不可重复审核")

    target = db.query(User).filter(User.id == app_row.user_id).first()
    if not target:
        raise HTTPException(404, "申请关联用户不存在")

    refresh_tier_configs(db)
    target.role = "pro"
    app_row.status = "approved"
    app_row.reviewed_at = datetime.now(timezone.utc)
    app_row.reviewed_by = owner.username or ""
    app_row.review_note = (body.note or "")[:255]
    db.commit()

    keys_upgraded = _upgrade_user_keys_to_pro(db, str(target.id))

    logger.info(
        "Pro 申请批准 id=%s user=%s by=%s keys=%s",
        app_row.id, target.username, owner.username, keys_upgraded,
    )
    try:
        from src.web.api.audit import log_audit

        log_audit(
            db,
            owner,
            "pro_approve",
            detail=f"批准 Pro 申请 id={app_row.id} user={target.username} keys={keys_upgraded}",
        )
    except Exception:  # noqa: BLE001
        pass

    return {
        "status": "approved",
        "application": _app_to_dict(app_row),
        "user": {"id": str(target.id), "username": target.username, "role": target.role},
        "keys_upgraded": keys_upgraded,
        "daily_limit": TIER_DAILY_LIMIT["pro"],
    }


@router.post("/pro/admin/reject")
@allow_cross_user  # C3(2026-09-18): owner-only(Depends(require_owner)) 的申请驳回, 按 application_id 定位, 有意跨用户
def admin_reject(
    body: ReviewRequest,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """拒绝 Pro 申请。不改用户 role / key。"""
    app_row = db.query(ProApplication).filter(ProApplication.id == body.application_id).first()
    if not app_row:
        raise HTTPException(404, "申请不存在")
    if app_row.status != "pending":
        raise HTTPException(400, f"申请已处理过(status={app_row.status}), 不可重复审核")

    app_row.status = "rejected"
    app_row.reviewed_at = datetime.now(timezone.utc)
    app_row.reviewed_by = owner.username or ""
    app_row.review_note = (body.note or "")[:255]
    db.commit()

    logger.info(
        "Pro 申请拒绝 id=%s user=%s by=%s",
        app_row.id, app_row.username, owner.username,
    )
    try:
        from src.web.api.audit import log_audit

        log_audit(
            db,
            owner,
            "pro_reject",
            detail=f"拒绝 Pro 申请 id={app_row.id} user={app_row.username}",
        )
    except Exception:  # noqa: BLE001
        pass

    return {
        "status": "rejected",
        "application": _app_to_dict(app_row),
    }


@router.get("/pro/admin/expired")
def admin_expired_keys(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=200, ge=1, le=1000),
    _owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """已到期降级的 key 列表(近 N 天 tier_downgrade_logs)。"""
    from datetime import timedelta

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        db.query(TierDowngradeLog)
        .filter(TierDowngradeLog.created_at >= since)
        .order_by(TierDowngradeLog.created_at.desc(), TierDowngradeLog.id.desc())
        .limit(limit)
        .all()
    )
    return {
        "days": days,
        "items": [
            {
                "id": r.id,
                "api_key_id": r.api_key_id,
                "key_prefix": r.key_prefix or "",
                "user_id": r.user_id,
                "from_tier": r.from_tier,
                "to_tier": r.to_tier,
                "from_daily_limit": r.from_daily_limit,
                "to_daily_limit": r.to_daily_limit,
                "reason": r.reason or "",
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
        "total_in_window": (
            db.query(TierDowngradeLog).filter(TierDowngradeLog.created_at >= since).count()
        ),
    }
