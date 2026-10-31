"""登录验证码接口（`GET /api/auth/captcha`）。

只做两件事：发一张一次性图片、把校验能力交给 `src/core/captcha.py`。
**绕过检测**：本路由不返回答案，也不缓存（`Cache-Control: no-store`）——
验证码被代理/CDN 缓存或写进日志，就等于没有。
"""
from __future__ import annotations

import base64
import logging

from fastapi import APIRouter, HTTPException, Response

from src.core import captcha as captcha_core

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/captcha")
async def issue_captcha(response: Response):
    """签发一次性图片验证码（180 秒有效、答对一次即作废）。"""
    if captcha_core.mode() == "off":
        # 明确告知前端"当前不需要验证码"，而不是发一张没人校验的图（那是假安全）。
        return {"enabled": False, "captcha_id": None, "image": None, "expires_in": 0}
    try:
        item = captcha_core.new_captcha()
        png = captcha_core.render_png(item.answer)
    except captcha_core.CaptchaUnavailable as e:
        # 图片发不出来就明确 503：绝不放行"没有验证码也能登录"。
        logger.error("[captcha] 生成失败: %s", e)
        raise HTTPException(503, "验证码服务不可用，请稍后重试") from e
    response.headers["Cache-Control"] = "no-store"
    return {
        "enabled": True,
        "captcha_id": item.captcha_id,
        "image": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
        "expires_in": captcha_core.TTL_SECONDS,
        "mode": captcha_core.mode(),
    }
