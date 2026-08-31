"""SIDA 扫码绑定个人微信(腾讯官方 iLink 直连) API。

链路: 设置页扫码 -> fetch_qr() 出二维码 -> 微信扫码确认 -> poll_qr() 轮询
      -> 凭证(token/base_url/user_id)存 notify_channels(type=wechat_ilink) -> 推送走 _send_wechat_ilink

凭证落 notify_channels.config: {account_id, token, base_url, user_id}
不建新表。
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.core import wechat_ilink
from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import NotifyChannel, User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/notify/wechat-bind")

CHANNEL_TYPE = "wechat_ilink"
CHANNEL_NAME = "个人微信(扫码绑定)"


def _find_bound_channel(db: Session, user: User) -> NotifyChannel | None:
    """当前用户扫码绑定的 wechat_ilink 渠道(带 account_id+user_id 的)。"""
    rows = (
        db.query(NotifyChannel)
        .filter(
            NotifyChannel.user_id == user.id,
            NotifyChannel.type == CHANNEL_TYPE,
        )
        .all()
    )
    for row in rows:
        cfg = row.config or {}
        if row.name == CHANNEL_NAME or (cfg.get("account_id") and cfg.get("user_id")):
            return row
    return None


@router.post("/start")
async def start_bind():
    """获取 iLink 扫码二维码。返回 {qrcode, qrcode_url}(qrcode 用于轮询)。"""
    try:
        qr = await wechat_ilink.fetch_qr()
    except Exception as exc:
        logger.error(f"iLink 获取二维码失败: {exc}")
        raise HTTPException(status_code=503, detail="微信扫码服务不可用, 请稍后重试")
    return {"qrcode": qr["qrcode"], "qrcode_url": qr["qrcode_url"]}


@router.get("/status")
async def bind_status(
    qrcode: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """轮询扫码状态。成功后自动保存 wechat_ilink 通知渠道(按 user 隔离)。"""
    try:
        result = await wechat_ilink.poll_qr(qrcode)
    except Exception as exc:
        logger.error(f"iLink 轮询二维码失败: {exc}")
        raise HTTPException(status_code=503, detail="微信扫码服务不可用, 请稍后重试")

    if result.get("status") != "success":
        return {"status": result.get("status", "wait")}

    account_id = result.get("account_id") or ""
    token = result.get("token") or ""
    base_url = result.get("base_url") or wechat_ilink.ILINK_BASE_URL
    user_id = result.get("user_id") or ""
    if not account_id or not token:
        logger.error(f"iLink 扫码确认但凭证不完整: {result}")
        raise HTTPException(status_code=502, detail="微信授权凭证不完整, 请重试")

    # upsert: 该用户已绑定则更新, 否则新建
    channel = _find_bound_channel(db, user)
    config = {
        "account_id": account_id,
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
    }
    if channel:
        channel.config = config
        channel.enabled = True
    else:
        channel = NotifyChannel(
            name=CHANNEL_NAME,
            type=CHANNEL_TYPE,
            config=config,
            enabled=True,
            is_default=False,
            user_id=user.id,
        )
        db.add(channel)
    db.commit()
    logger.info(f"用户 {user.id} 微信扫码绑定成功 account_id={account_id}")
    return {"status": "success", "account_id": account_id, "user_id": user_id}


@router.get("")
async def get_bind(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """当前用户绑定状态。"""
    channel = _find_bound_channel(db, user)
    if not channel:
        return {"bound": False, "account_id": None, "user_id": None}
    cfg = channel.config or {}
    return {
        "bound": True,
        "account_id": cfg.get("account_id"),
        "user_id": cfg.get("user_id"),
    }


@router.delete("")
async def unbind(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """解除绑定(删除该用户扫码绑定的 wechat_ilink 渠道)。"""
    channel = _find_bound_channel(db, user)
    if channel:
        db.delete(channel)
        db.commit()
    return {"ok": True, "unbound": bool(channel)}
