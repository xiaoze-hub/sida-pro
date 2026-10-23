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


class TokenResult(tuple):
    """`(token, expires_at)` 二元组, 额外携带 `jti` / `payload`。

    2026-10-23 P0(设备限制失效): 会话表此前用 `token[:32]` 当 session_id ——
    JWT 头部恒为 `{"alg":"HS256","typ":"JWT"}`, 前 32 字符全平台完全相同,
    所有用户所有设备覆写同一行, `enforce_device_limit` 永远看不到多行。
    现以 payload 的 `jti`(每次签发唯一)作 session_id。

    继承 tuple 是为保持既有 `token, expires_at = create_token(user)` 调用不破
    (仓库大量测试/调用点这么解包), 同时允许 `create_token(user).jti` 取值。
    """

    # 不声明 __slots__: tuple 子类因此获得 __dict__, 才能挂 jti/token 属性。
    token: str
    expires_at: datetime
    jti: str
    payload: dict

    def __new__(cls, token: str, expires_at: datetime, jti: str, payload: dict) -> "TokenResult":
        obj = super().__new__(cls, (token, expires_at))
        obj.token = token
        obj.expires_at = expires_at
        obj.jti = jti
        obj.payload = payload
        return obj


def create_token(user: User, expires_hours: int = JWT_EXPIRE_HOURS) -> TokenResult:
    """创建 JWT token, 含 user_id + role + ver(踢人用)。始终用当前 secret 签发。

    返回 `TokenResult`: 可解包为 `(token, expires_at)`, 也可直接取 `.jti` /
    `.payload`(设备会话表用 jti 作唯一 session_id)。
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=expires_hours)
    jti = secrets.token_hex(16)
    payload = {
        "exp": expires_at,
        "iat": now,
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "jti": jti,
        "ver": user.token_version,
    }
    token = jwt.encode(payload, get_jwt_secret(), algorithm=JWT_ALGORITHM)
    return TokenResult(token, expires_at, jti, payload)


def session_id_from_payload(payload: dict | None) -> str | None:
    """从 JWT payload 取会话 id(jti)。缺失/空 → None(老 token 无 jti)。

    调用方必须把 None 当「无法判定」处理(fail-open), 不得因此拒绝请求。
    """
    if not payload:
        return None
    jti = payload.get("jti")
    if not jti:
        return None
    return str(jti)


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
