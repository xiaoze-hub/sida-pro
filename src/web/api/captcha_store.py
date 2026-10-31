"""验证码挑战的服务端存储（DB）+ 机器身份豁免（2026-10-31）。

为什么单独一层：
- `src/core/captcha.py` 只做"签发 token / 渲染图片 / 判定模式"，**不碰数据库**，
  也不允许反向依赖 `src/web`（B4.1 门禁，实测被 CI 抓过）；
- 答案摘要要落库（多 worker 共享、一次性消费、过期清理），这是数据访问，属于 web 层。
"""
from __future__ import annotations

import hmac
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from src.core import captcha as captcha_core
from src.db.models import CaptchaChallenge

logger = logging.getLogger(__name__)

# 过期行保留多久再清（够排障、也不让表无限长）。只在发码时顺手清，不开定时任务。
PRUNE_AFTER_SECONDS = 3600


def store_challenge(db, item: captcha_core.Captcha, *, now: datetime | None = None) -> None:
    """登记一张挑战（答案只存摘要）。写失败必须抛出：没记下来的码永远验不过。"""
    moment = now or datetime.utcnow()
    db.add(CaptchaChallenge(
        nonce=item.nonce,
        answer_digest=captcha_core.answer_digest(item.answer),
        expires_at=moment + timedelta(seconds=captcha_core.TTL_SECONDS),
    ))
    db.commit()
    _prune(db, moment)


def consume_challenge(db, nonce: str, answer: str | None, *, now: datetime | None = None) -> tuple[bool, str]:
    """校验并**一次性消费**：无论对错都删行（答错也烧掉，逼着重新看图）。

    返回 `(是否通过, 原因码)`。
    """
    moment = now or datetime.utcnow()
    row = db.query(CaptchaChallenge).filter(CaptchaChallenge.nonce == nonce).first()
    if row is None:
        return False, "unknown"
    try:
        db.delete(row)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.warning("[captcha] 消费挑战失败(按未通过处理)", exc_info=True)
        return False, "store_error"
    if row.expires_at and row.expires_at <= moment:
        return False, "expired"
    if not answer:
        return False, "missing"
    if not hmac.compare_digest(row.answer_digest, captcha_core.answer_digest(answer)):
        return False, "mismatch"
    return True, "ok"


def _prune(db, now: datetime) -> None:
    cutoff = now - timedelta(seconds=PRUNE_AFTER_SECONDS)
    try:
        db.execute(delete(CaptchaChallenge).where(CaptchaChallenge.expires_at < cutoff))
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.debug("[captcha] 清理过期挑战失败(忽略)", exc_info=True)


def service_request_exempt(headers) -> bool:
    """带**有效服务令牌**的请求豁免验证码（预测引擎/内部脚本没有屏幕）。

    与 `auth.py` 的 `X-Service-Token` 同源同值，常量时间比较；令牌未配置则**没有**任何豁免。
    """
    try:
        from src.web.api.auth import SERVICE_TOKEN_HEADER, get_service_token

        expected = (get_service_token() or "").strip()
        supplied = str(headers.get(SERVICE_TOKEN_HEADER) or "").strip()
        return bool(expected and supplied) and hmac.compare_digest(supplied, expected)
    except Exception:  # noqa: BLE001 - 取不到令牌就不豁免
        return False
