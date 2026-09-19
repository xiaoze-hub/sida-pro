"""邀请码管理(owner only) —— 内部使用模式的准入控制台。

2026-09-19 用户拍板: 产品内部使用, 注册改邀请制。本路由给管理员生成/查看/作废邀请码,
并查询**使用审计**(谁在什么时候用什么 IP 用了哪个码)。

权限: 全部 `require_owner`。普通登录用户访问一律 403 —— 邀请码是准入凭证, 不是普通数据。
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.invite_codes import (
    InviteCodeError,
    create_invite_code,
    disable_invite_code,
    get_register_mode,
    list_invite_codes,
    list_uses,
    mask_code,
    redeem,
)
from src.web.api.audit import log_audit
from src.web.api.auth import require_owner
from src.web.database import get_db
from src.web.models import User

router = APIRouter()


class CreateInviteCodeRequest(BaseModel):
    note: str = ""
    max_uses: int = 1
    expires_in_days: Optional[int] = None
    #: 允许管理员指定固定码(便于线下口头发放), 不传则随机生成
    code: Optional[str] = None


@router.get("/invite-codes/mode")
def invite_mode(db: Session = Depends(get_db), owner: User = Depends(require_owner)):
    """当前注册模式(invite/open/closed) + 邀请码统计。"""
    codes = list_invite_codes(db, limit=500)
    active = [c for c in codes if not c["disabled"] and c["remaining"] > 0]
    return {
        "mode": get_register_mode(db),
        "total": len(codes),
        "active": len(active),
        "used_total": sum(c["used_count"] for c in codes),
    }


@router.post("/invite-codes")
def create_code(
    data: CreateInviteCodeRequest,
    request: Request,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
):
    """生成邀请码(返回明文码, 只在这一次返回)。"""
    try:
        item = create_invite_code(
            db,
            created_by=owner.username or "",
            note=data.note or "",
            max_uses=data.max_uses or 1,
            expires_in_days=data.expires_in_days,
            code=data.code,
        )
    except InviteCodeError as e:
        raise HTTPException(400, e.message)
    log_audit(
        db,
        owner,
        "invite_code.create",
        f"生成邀请码 {mask_code(item['code'])} 可用{item['max_uses']}次 note={data.note or '-'}",
        request.client.host if request.client else "",
    )
    return item


@router.get("/invite-codes")
def list_codes(
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
):
    """邀请码列表(含已用次数/剩余/是否停用)。"""
    return {"items": list_invite_codes(db, limit=limit), "mode": get_register_mode(db)}


@router.get("/invite-codes/uses")
def code_uses(
    code: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
):
    """**使用审计**: 哪个码、谁用的、什么时候、什么 IP。"""
    return {"items": list_uses(db, code=code, limit=limit)}


@router.post("/invite-codes/{code}/disable")
def disable_code(
    code: str,
    request: Request,
    db: Session = Depends(get_db),
    owner: User = Depends(require_owner),
):
    """作废邀请码(已核销的记录不动, 审计保留)。"""
    ok = disable_invite_code(db, code)
    if not ok:
        raise HTTPException(404, "邀请码不存在或已停用")
    log_audit(
        db,
        owner,
        "invite_code.disable",
        f"停用邀请码 {mask_code(code)}",
        request.client.host if request.client else "",
    )
    return {"disabled": True, "code": code}


#: 供 register 流程复用(保持单一实现, 避免两处核销逻辑分叉)
__all__ = ["router", "redeem"]
