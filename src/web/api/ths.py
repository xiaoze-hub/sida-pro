"""同花顺登录态 API: 扫码登录二维码 / 状态轮询 / 登录态查询 / 登出 / 账号维护摘要。

端点:
    POST /api/ths/qrcode        生成扫码登录二维码(返回 base64 图 + qrid)
    GET  /api/ths/qrcode/{qrid} 轮询扫码状态(成功返回凭证并自动登录持久化)
    GET  /api/ths/session       当前登录态(自动续期)
    POST /api/ths/logout        清除登录态 + GET /api/ths/account 账号维护摘要
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from src.core.ths_auth import (
    create_qrcode,
    clear_session,
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


@router.post("/logout")
async def ths_logout():
    """清除同花顺登录态(删持久化凭证, SDK 回落游客模式)。"""
    try:
        clear_session()
        return {"logged_in": False, "message": "已登出,SDK 回落游客模式"}
    except Exception as e:
        logger.warning(f"[ths] 登出失败: {e}")
        raise HTTPException(502, f"登出失败: {e}")


@router.get("/account")
async def ths_account():
    """同花顺账号维护摘要: SDK 模式(正式/游客) + 扫码登录态 + 已验证能力。

    mode 判定与 THSDKL2 一致: THS_USERNAME/PASSWORD 有无。
    capabilities 为 2026-09-05 生产实测结论(非实时探测, 实时探测走 /datasources)。
    """
    import os

    formal = bool(os.environ.get("THS_USERNAME") and os.environ.get("THS_PASSWORD"))
    try:
        sess = session_status()
    except Exception:
        sess = {"logged_in": False}
    return {
        "mode": "formal" if formal else "guest",
        "mode_label": "正式账户" if formal else "游客模式",
        "session": sess,
        "capabilities": [
            {"key": "dde_official", "label": "DDE官方分档(query_data)", "ok": True},
            {"key": "ext1", "label": "扩展1主力净流入", "ok": formal},
            {"key": "orderbook20", "label": "20档盘口", "ok": True},
            {"key": "l2ticks", "label": "L2逐笔", "ok": True},
            {"key": "auction", "label": "竞价异动", "ok": True},
            {"key": "bigorders", "label": "大单流向", "ok": formal},
            {"key": "hk_us", "label": "港美行情", "ok": formal},
        ],
        "note": "游客模式下扩展1/港美/大单返0行, 需正式账户解锁",
    }


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
