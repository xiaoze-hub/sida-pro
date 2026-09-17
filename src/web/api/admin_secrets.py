"""管理端密钥轮换 API (P0, 2026-09-18)

- POST /api/admin/rotate-secrets  owner 手动触发 JWT_SECRET 轮换
- GET  /api/admin/secrets/status  owner 查看轮换状态(不含 secret 明文)

路由挂载: app.include_router(..., prefix="/api/admin") → 实际路径
/api/admin/rotate-secrets、/api/admin/secrets/status。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request

from src.web.api.auth import require_owner
from src.web.models import User

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/rotate-secrets")
async def rotate_secrets(
    request: Request,
    owner: User = Depends(require_owner),
):
    """手动轮换 JWT_SECRET(owner only)。

    - 生成新密钥并立即用于新签发 token
    - 旧密钥进入 grace period(默认 7 天)仍可验签, 在线会话不掉线
    - 落 audit_logs(action=rotate_jwt_secret)
    - env JWT_SECRET pin 时返回 rotated=false 并说明原因
    """
    from src.core.secret_rotation import rotate_jwt_secret

    ip = request.client.host if request.client else ""
    try:
        result = rotate_jwt_secret(actor=f"owner:{owner.username}")
    except Exception as e:  # noqa: BLE001
        logger.exception("[admin_secrets] 轮换失败")
        from fastapi import HTTPException
        raise HTTPException(500, f"密钥轮换失败: {e}")

    try:
        from src.web.api.audit import log_audit
        log_audit(
            db=None,
            user=owner,
            action="admin_rotate_secrets",
            detail=f"手动触发 JWT 轮换: {result}",
            ip=ip,
        )
    except Exception:  # noqa: BLE001
        pass

    return result


@router.get("/secrets/status")
async def secrets_status(owner: User = Depends(require_owner)):
    """轮换状态(owner only): 间隔/grace/上次轮换/剩余天数/env pin。"""
    from src.core.secret_rotation import get_rotation_status

    return get_rotation_status()
