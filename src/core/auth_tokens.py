"""JWT 令牌与密钥(中立层, KI-039 第二阶段, 2026-09-09)。

从 `src/web/api/auth.py` 下沉: `get_jwt_secret` / `create_token` / `decode_token`
是纯认证原语, core 侧(startup_check / wechat_bot_worker)也要用, 不应依赖 Web 层。

密钥解析顺序: env `JWT_SECRET` → `AppSettings.jwt_secret`(缺则生成并落库)。

P0(2026-09-18)密钥轮换: decode 支持多 secret 验证(当前 + grace period 内的旧密钥)。
新 token 一律用当前 secret 签发; 旧 token 在旧 secret 过期前仍可验证。
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from src.db.models import AppSettings, User
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "12"))
JWT_SECRET_KEY = "jwt_secret"
AUTH_TOKEN_VERSION_KEY = "auth_token_version"

# JWT Secret 缓存
_jwt_secret: str | None = None


def invalidate_jwt_secret_cache() -> None:
    """清空进程内 secret 缓存(密钥轮换后必须调用, 否则继续用旧值签发/验签)。"""
    global _jwt_secret
    _jwt_secret = None


def get_jwt_secret() -> str:
    """获取当前 JWT Secret(env 优先, 否则持久化到数据库)。"""
    global _jwt_secret
    if _jwt_secret:
        return _jwt_secret

    # 环境变量优先(仅当长度足够)
    env_secret = os.getenv("JWT_SECRET")
    if env_secret:
        # P1(audit-20260915): RFC 7518 HS256 建议 >=32 字节; 短密钥不安全,
        # 忽略并走下方随机生成路径(既有短密钥会话将失效, 请尽快配置 32+ 字节)。
        if len(env_secret.encode("utf-8")) >= 32:
            _jwt_secret = env_secret
            return _jwt_secret
        logger.warning(
            "JWT_SECRET 仅 %d 字节 (<32, RFC 7518 HS256 建议), 已忽略并自动生成随机密钥; "
            "请尽快配置 32+ 字节 JWT_SECRET",
            len(env_secret.encode("utf-8")),
        )

    # 从数据库读取或首次生成(随机 32 字节 hex 密钥)
    db = SessionLocal()
    try:
        setting = db.query(AppSettings).filter(AppSettings.key == JWT_SECRET_KEY).first()
        if setting:
            _jwt_secret = setting.value
        else:
            _jwt_secret = secrets.token_hex(32)
            db.add(
                AppSettings(
                    key=JWT_SECRET_KEY,
                    value=_jwt_secret,
                    description="JWT签名密钥(自动生成)",
                )
            )
            db.commit()
        return _jwt_secret
    finally:
        db.close()


def get_verification_secrets() -> list[str]:
    """返回所有可用于验签的 secret(当前优先, 之后是 grace period 内的旧密钥)。

    失败降级为仅当前 secret, 不阻断鉴权。
    """
    secrets_list: list[str] = [get_jwt_secret()]
    try:
        from src.core.secret_rotation import get_grace_secrets

        for old in get_grace_secrets():
            if old and old not in secrets_list:
                secrets_list.append(old)
    except Exception as e:  # noqa: BLE001 - 轮换模块缺失/DB 异常不阻断验签
        logger.debug("[auth_tokens] 读取轮换旧密钥失败(仅用当前 secret): %s", e)
    return secrets_list


def create_token(user: User, expires_hours: int = JWT_EXPIRE_HOURS) -> tuple[str, datetime]:
    """创建 JWT token, 含 user_id + role + ver(踢人用)。始终用当前 secret 签发。"""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=expires_hours)
    payload = {
        "exp": expires_at,
        "iat": now,
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "jti": secrets.token_hex(16),
        "ver": user.token_version,
    }
    token = jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)
    return token, expires_at


def decode_token(token: str) -> dict | None:
    """解码 JWT, 失败返回 None。

    P0(2026-09-18): 依次尝试当前 secret 与 grace period 内的旧 secret,
    保证轮换窗口内已签发 token 不掉线。过期 token 直接返回 None(与 secret 无关)。
    """
    if not token:
        return None
    for secret in get_verification_secrets():
        try:
            return jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            continue
    return None


def principal_from_payload(payload: dict | None) -> dict:
    """JWT payload → request.state.user 的统一形状。

    用户 id 在 sub 字段(create_token); 历史上限流/审计两处中间件各写各的
    取法, 限流处取了不存在的 user_id claim → 分桶恒按 IP, 已登录互拖。
    """
    if not payload:
        return {}
    return {
        "user_id": payload.get("sub") or payload.get("user_id"),
        "username": payload.get("username") or "",
        "role": payload.get("role") or "",
    }
