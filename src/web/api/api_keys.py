"""API Key 管理控制台(2026-09-16): 登录用户自助管理自己的 Skill API Key。

路由(挂 /api/keys 前缀, 全部需登录 JWT):
- GET    /api/keys                 列出当前用户所有 Key(不含明文)
- POST   /api/keys                 创建新 Key(tier=free) — 见下方路由顺序说明
- POST   /api/keys/{key_id}/reset  重置 Key(生成新 sk_ 明文, 旧 key 立即失效)
- DELETE /api/keys/{key_id}        软删(status=disabled)
- GET    /api/keys/{key_id}/usage  单 Key 用量统计

红线:
- 全部操作 user_id 隔离: 只能读写 user_id == 当前登录用户的 key
- 列表/重置响应不含明文(创建/重置时明文仅返回一次)
- 重置 = 换 key_hash + key_prefix, 鉴权按 hash 查表 → 旧明文立即 401
- 不破坏既有 POST /api/keys(guest 领取 + 可选 JWT 绑定): 本模块挂在
  skills_gateway 之后, 同路径 POST 仍由 skills_gateway.register_key 处理;
  登录用户走该端点同样会得到绑定 user_id 的 free key。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from src.db.models import SkillApiKey, SkillUsage, User
from src.web.api.auth import get_current_user
from src.web.api.skills_gateway import (
    TIER_DAILY_LIMIT,
    _gen_key,
    _hash_key,
    refresh_tier_configs,
)
from src.web.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter(tags=["api-keys"])


def _key_to_dict(row: SkillApiKey) -> dict:
    """Key 序列化(不含明文/hash)。"""
    return {
        "id": row.id,
        "key_prefix": row.key_prefix,
        "owner_label": row.owner_label or "",
        "tier": row.tier,
        "status": row.status,
        "daily_limit": row.daily_limit,
        "frozen_reason": row.frozen_reason or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


def _get_owned_key(db: Session, user: User, key_id: int) -> SkillApiKey:
    """按 id 取 key 并校验归属; 不存在或非本人一律 404(不泄露他人 key 是否存在)。"""
    row = (
        db.query(SkillApiKey)
        .filter(SkillApiKey.id == key_id, SkillApiKey.user_id == user.id)
        .first()
    )
    if not row:
        raise HTTPException(404, "API Key 不存在")
    return row


@router.get("")
def list_keys(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """列出当前用户全部 API Key(不含明文)。"""
    rows = (
        db.query(SkillApiKey)
        .filter(SkillApiKey.user_id == user.id)
        .order_by(SkillApiKey.created_at.desc())
        .all()
    )
    return {"keys": [_key_to_dict(r) for r in rows]}


@router.post("")
def create_key(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """创建新 Key(tier=free)。明文只在本次响应返回一次。

    说明: 线上同路径 POST /api/keys 由 skills_gateway.register_key 优先匹配
    (guest 领取 + JWT 绑定, 向后兼容)。本端点是登录态控制台语义(强制 free、
    无 trial 分支), 在路由表中作为兜底; 两者行为对登录用户等价。
    """
    refresh_tier_configs(db)
    raw = _gen_key()
    row = SkillApiKey(
        key_hash=_hash_key(raw),
        key_prefix=raw[:11],
        owner_label=user.username or "",
        user_id=user.id,
        tier="free",
        status="active",
        daily_limit=TIER_DAILY_LIMIT["free"],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info("[api-keys] user %s created key id=%s", user.username, row.id)
    return {
        "api_key": raw,  # 仅此一次
        "key": _key_to_dict(row),
        "note": "请妥善保存 API Key, 服务端不会再次展示明文。",
    }


@router.post("/{key_id}/reset")
def reset_key(
    key_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """重置 Key: 生成新的 sk_ 明文, 替换 key_hash/key_prefix。

    鉴权按 key_hash 查表, 旧明文 hash 不再命中 → 立即失效。
    """
    row = _get_owned_key(db, user, key_id)
    if row.status == "disabled":
        raise HTTPException(400, "已删除的 Key 不能重置")
    raw = _gen_key()
    row.key_hash = _hash_key(raw)
    row.key_prefix = raw[:11]
    if row.status == "frozen":
        # 重置视为用户主动换 key, 一并解冻(异常用量绑定的是旧 hash)
        row.status = "active"
        row.frozen_reason = ""
    db.commit()
    db.refresh(row)
    logger.info("[api-keys] user %s reset key id=%s", user.username, row.id)
    return {
        "api_key": raw,  # 仅此一次
        "key": _key_to_dict(row),
        "note": "旧 API Key 已失效, 请改用新 Key。",
    }


@router.delete("/{key_id}")
def delete_key(
    key_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """删除 Key(软删: status=disabled, 行保留以便用量审计)。"""
    row = _get_owned_key(db, user, key_id)
    if row.status == "disabled":
        return {"id": row.id, "status": row.status, "message": "Key 已是删除状态"}
    row.status = "disabled"
    row.frozen_reason = row.frozen_reason or "用户删除"
    db.commit()
    logger.info("[api-keys] user %s disabled key id=%s", user.username, row.id)
    return {"id": row.id, "status": row.status, "message": "Key 已删除"}


@router.get("/{key_id}/usage")
def key_usage(
    key_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """单 Key 用量统计: 今日/累计调用、按日近 7 天、按 channel 今日。"""
    row = _get_owned_key(db, user, key_id)
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = day_start - timedelta(days=6)

    today_count = (
        db.query(func.count(SkillUsage.id))
        .filter(SkillUsage.api_key_id == row.id, SkillUsage.created_at >= day_start)
        .scalar()
        or 0
    )
    total_count = (
        db.query(func.count(SkillUsage.id))
        .filter(SkillUsage.api_key_id == row.id).scalar() or 0
    )

    # 近 7 天按日(兼容 SQLite/PG: 用日期字符串分组)
    daily_rows = (
        db.query(func.date(SkillUsage.created_at).label("d"), func.count(SkillUsage.id))
        .filter(SkillUsage.api_key_id == row.id, SkillUsage.created_at >= week_start)
        .group_by(func.date(SkillUsage.created_at))
        .order_by(func.date(SkillUsage.created_at))
        .all()
    )
    daily = {str(d): int(c) for d, c in daily_rows}

    # 今日按 channel(旧数据 channel 可能空, 归 api)
    ch_rows = (
        db.query(SkillUsage.channel, func.count(SkillUsage.id))
        .filter(SkillUsage.api_key_id == row.id, SkillUsage.created_at >= day_start)
        .group_by(SkillUsage.channel)
        .all()
    )
    by_channel: dict[str, int] = {}
    for ch, c in ch_rows:
        key = (ch or "api").strip() or "api"
        by_channel[key] = by_channel.get(key, 0) + int(c)

    return {
        "key": _key_to_dict(row),
        "used_today": int(today_count),
        "remaining_today": max(0, int(row.daily_limit) - int(today_count)),
        "total_calls": int(total_count),
        "daily_last_7d": daily,
        "by_channel_today": by_channel,
    }
