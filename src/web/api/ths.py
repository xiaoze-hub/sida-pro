"""同花顺登录态 API: 扫码登录二维码 / 状态轮询 / 登录态查询。

端点:
    POST /api/ths/qrcode        生成扫码登录二维码(返回 base64 图 + qrid)
    GET  /api/ths/qrcode/{qrid} 轮询扫码状态(成功返回凭证并自动登录持久化)
    GET  /api/ths/session       当前登录态(自动续期)
    POST /api/ths/logout        清除登录态
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from src.core.ths_auth import (
    create_qrcode,
    login,
    poll_qrcode,
    save_session,
    session_status,
    get_session,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ths"])


@router.post("/qrcode")
async def ths_create_qrcode():
    """生成同花顺扫码登录二维码。"""
    try:
        qr = create_qrcode()
        return {"qrid": qr["qrid"], "img_base64": qr["img_base64"], "created_at": qr["created_at"]}
    except Exception as e:
        logger.warning(f"[ths] 生成二维码失败: {e}")
        raise HTTPException(502, f"生成二维码失败: {e}")


@router.get("/qrcode/{qrid}")
async def ths_poll_qrcode(qrid: str):
    """轮询扫码状态。成功: 自动登录 + 持久化凭证。"""
    try:
        cred = poll_qrcode(qrid, timeout_s=30)
        sess = login(cred["account"], cred["password"])
        save_session(sess)
        return {
            "logged_in": True,
            "account": sess.account,
            "userid": sess.userid,
            "sessionid": sess.sessionid[:16] + "...",
            "expires": sess.expires.isoformat() if sess.expires else None,
            "passport_ok": bool(sess.passport),
        }
    except TimeoutError as e:
        raise HTTPException(408, str(e))
    except Exception as e:
        logger.warning(f"[ths] 扫码轮询失败: {e}")
        raise HTTPException(502, f"扫码轮询失败: {e}")


@router.get("/session")
async def ths_session():
    """当前登录态(自动续期)。"""
    try:
        sess = get_session()
        if not sess.logged_in:
            return session_status()
        return {
            "logged_in": True,
            "account": sess.account,
            "userid": sess.userid,
            "sessionid": sess.sessionid[:16] + "...",
            "expires": sess.expires.isoformat() if sess.expires else None,
            "passport_ok": bool(sess.passport),
        }
    except Exception as e:
        logger.warning(f"[ths] 查询登录态失败: {e}")
        raise HTTPException(502, f"查询登录态失败: {e}")
