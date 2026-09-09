"""认证 API - 多用户 JWT 认证(2026-08-10 阶段1)

- users 表(UUID 主键), role: owner|member
- 兼容旧单用户: 首次启动自动从 AppSettings 迁移旧账号为 owner
- JWT payload: user_id + role + ver(踢人用)
- 权限依赖: get_current_user(登录) / require_owner(仅管理员)
"""
import os
import hashlib
import hmac
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from sqlalchemy.orm import Session
import jwt

from src.web.database import get_db, SessionLocal
from src.web.models import AppSettings, User
from src.core.auth_tokens import (  # noqa: F401  (KI-039 第二阶段: 原语下沉 core)
    AUTH_TOKEN_VERSION_KEY,
    JWT_ALGORITHM,
    JWT_EXPIRE_HOURS,
    JWT_SECRET_KEY,
    create_token,
    decode_token,
    get_jwt_secret,
    principal_from_payload,
)

router = APIRouter()
logger = logging.getLogger(__name__)
security = HTTPBearer(auto_error=False)

# JWT 配置
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.getenv("JWT_EXPIRE_HOURS", "12"))

# 环境变量配置（Docker 部署用）— 惰性读取: 允许测试/调用方在 import 后注入 env
ENV_AUTH_USERNAME_KEY = "AUTH_USERNAME"
ENV_AUTH_PASSWORD_KEY = "AUTH_PASSWORD"


def _env_credentials() -> tuple[str | None, str | None]:
    return os.getenv(ENV_AUTH_USERNAME_KEY), os.getenv(ENV_AUTH_PASSWORD_KEY)

# 设置项 key(旧单用户兼容)
AUTH_USERNAME_KEY = "auth_username"
PASSWORD_HASH_KEY = "auth_password_hash"
JWT_SECRET_KEY = "jwt_secret"
AUTH_TOKEN_VERSION_KEY = "auth_token_version"

# JWT Secret 缓存
_jwt_secret: str | None = None


# get_jwt_secret/create_token/decode_token/principal_from_payload
# 已下沉 src/core/auth_tokens.py(KI-039 第二阶段); 见文件顶部 import。

class LoginRequest(BaseModel):
    username: str
    password: str


class SetupRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class RegisterRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    expires_at: str
    user: Optional[dict] = None


# P1-6 (2026-08-23 审计): scrypt 提参到 n=2^15 (中等强度, 单机 ~120ms/次)
# - 不拉满 2^17 是避免登录变慢 (实测 2^17 在登录路径会让 P95 超过 800ms)
# - 仍受 P1-7 透明重哈希保护: 旧 n=2^14 / SHA-256 哈希会在登录成功后
#   升级到 n=2^15, 增量迁移不强制用户改密。
SCRYPT_N_NEW = 2**15
SCRYPT_N_OLD = 2**14  # 兼容已存在的旧 scrypt$ 哈希 (不要随意提高, 登录会校验失败)


def hash_password(password: str) -> str:
    """使用标准库 scrypt + 随机盐保存密码(新哈希一律 n=2^15)。

    maxmem=2**26 (64 MiB): 显式放宽 OpenSSL 默认 32 MiB 上限, 否则 n=2^15+r=8
    刚好顶到默认上限 (128*n*r = 33 MiB) 在部分环境 (测试/容器 OpenSSL 较紧)
    会抛 "memory limit exceeded"。2**26 既覆盖 n=2^15 也为将来 n=2^16 留余量。
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N_NEW, r=8, p=1, maxmem=2**26,
    )
    return f"scrypt${SCRYPT_N_NEW}${salt.hex()}${digest.hex()}"


def needs_rehash(stored: str) -> bool:
    """P1-7: 是否需要透明升级到新 scrypt 参数。

    旧 n=2^14 (scrypt$...)  → True (升级到 n=2^15)
    旧 SHA-256 (无 scrypt$ 前缀) → True (升级到 scrypt)
    新 n=2^15 (scrypt$...) → False
    """
    if not stored.startswith("scrypt$"):
        return True  # 旧 SHA-256 路径
    try:
        parts = stored.split("$")
        # 旧格式可能没存 n, 全部按需升级稳妥
        return True if len(parts) < 4 else parts[1] != str(SCRYPT_N_NEW)
    except Exception:
        return True


def verify_password(password: str, stored: str) -> bool:
    """校验 scrypt (新旧参数自动识别), 兼容旧版 SHA-256 哈希。

    P1-7: 校验成功并不保证 stored 是新参数 —— 调用方应据 needs_rehash 决定是否
    透明升级并落库 (本函数不写库, 保持纯函数语义)。
    """
    if stored.startswith("scrypt$"):
        try:
            parts = stored.split("$")
            salt_hex = parts[2] if len(parts) >= 4 else parts[1]
            digest_hex = parts[3] if len(parts) >= 4 else parts[2]
            # 兼容: 若存了 n, 旧 n=2^14 用 SCRYPT_N_OLD 校验
            try:
                n_param = int(parts[1]) if len(parts) >= 4 else SCRYPT_N_OLD
            except ValueError:
                n_param = SCRYPT_N_OLD
            salt = bytes.fromhex(salt_hex)
            digest = hashlib.scrypt(
                password.encode("utf-8"), salt=salt, n=n_param, r=8, p=1, maxmem=2**26,
            )
            return hmac.compare_digest(digest.hex(), digest_hex)
        except Exception:
            return False
    # 旧版 SHA-256(无盐, 兼容迁移前数据)
    return hmac.compare_digest(hashlib.sha256(password.encode("utf-8")).hexdigest(), stored)


# ── 用户管理(多用户核心) ──────────────────────────────────────────────

def init_auth_from_env(db: Session) -> bool:
    """兼容旧接口(server.py 引用): 从环境变量初始化认证。

    多用户下由 get_or_create_owner 统一处理, 此函数仅确保 owner 存在。
    """
    owner = get_or_create_owner(db)
    return owner is not None


def _audit_owner_init(action: str, source: str, username: str) -> None:
    """P2-4 (2026-08-23 审计): owner 账号初始化(从 env / 旧单用户迁移 / 兜底创建)
    写一条 audit_logs (best-effort, 失败静默), 留痕"首次启动账号来源"。
    log_audit 接受 user=None 表示系统任务, username 字段填来源描述。
    """
    try:
        from src.web.api.audit import log_audit
        log_audit(
            db=None,  # log_audit 内部用独立 SessionLocal, 不需要外部 db
            user=None,  # 系统任务, owner 尚未就绪
            action=action,
            detail=f"source={source}, username={username}",
            ip="",
        )
    except Exception:
        pass


def get_or_create_owner(db: Session) -> User:
    """确保存在 owner 用户。

    首次启动: 从环境变量或旧单用户(AppSettings)迁移账号为 owner;
    若都没有, 创建默认 admin 账号并生成随机一次性密码打印到 stdout
    (强制运维首次登录后改密, P2-5 2026-08-23 审计)。
    """
    owner = db.query(User).filter(User.role == "owner").first()
    if owner:
        return owner

    # 1. 环境变量优先
    env_user, env_pass = _env_credentials()
    if env_user and env_pass:
        user = User(
            id=str(uuid.uuid4()),
            username=env_user,
            password_hash=hash_password(env_pass),
            role="owner",
        )
        db.add(user)
        db.commit()
        # P2-4: 首次启动 owner 初始化留痕
        _audit_owner_init("init_owner_from_env", "env", env_user)
        return user

    # 2. 旧单用户迁移(AppSettings)
    setting_username = db.query(AppSettings).filter(AppSettings.key == AUTH_USERNAME_KEY).first()
    setting_hash = db.query(AppSettings).filter(AppSettings.key == PASSWORD_HASH_KEY).first()
    if setting_username and setting_hash and setting_hash.value:
        user = User(
            id=str(uuid.uuid4()),
            username=setting_username.value,
            password_hash=setting_hash.value,  # 复用已有哈希(scrypt或旧sha256均可验)
            role="owner",
        )
        db.add(user)
        db.commit()
        # P2-4: 旧单用户迁移留痕
        _audit_owner_init("init_owner_from_appsettings", "appsettings_migration", setting_username.value)
        return user

    # 3. 兜底首启(裸库无 env 无旧数据): 2026-09-08 0.6 删除固定默认密码与其开关 ——
    #    公开仓库里源码内固定密码=失守入口。
    #    改为随机强密码, 与 JWT secret 缺失时自动生成的既有做法一致, 仅在启动日志打印一次。
    import logging as _logging

    _log = _logging.getLogger(__name__)
    generated_password = secrets.token_urlsafe(12)
    user = User(
        id=str(uuid.uuid4()),
        username="admin",
        password_hash=hash_password(generated_password),
        role="owner",
    )
    db.add(user)
    db.commit()
    _audit_owner_init("init_owner_generated", "fallback_generated", "admin")
    # 随机密码只在启动日志出现一次(Docker logs 可见), 首登后请立即改密。
    import sys as _sys
    _banner = "=" * 70
    print(
        f"\n{_banner}\n"
        "[首次启动] 已创建 owner 账号 (admin), 本次启动随机生成的密码:\n"
        f"[首次启动]   {generated_password}\n"
        "[首次启动] 仅此一次打印, 请立即登录并前往 '设置 → 修改密码' 改密。\n"
        "[首次启动] 生产环境推荐用 AUTH_USERNAME/AUTH_PASSWORD 环境变量注入。\n"
        f"{_banner}",
        file=_sys.stderr,
        flush=True,
    )
    _log.warning("[安全] 已创建随机密码 owner 账号 admin(密码见启动日志, 仅打印一次)")
    return user


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.query(User).filter(User.username == username).first()


def get_user_by_id(db: Session, user_id: str) -> User | None:
    return db.query(User).filter(User.id == user_id).first()


def create_user(db: Session, username: str, password: str, role: str = "member") -> User:
    """创建用户(owner 调用)。"""
    user = User(
        id=str(uuid.uuid4()),
        username=username,
        password_hash=hash_password(password),
        role=role,
    )
    db.add(user)
    db.commit()
    return user


# ── Token ─────────────────────────────────────────────────────────────

# ── 权限依赖 ──────────────────────────────────────────────────────────

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """验证当前用户(用作依赖), 返回 User 对象。"""
    owner = get_or_create_owner(db)  # 确保 owner 存在

    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(credentials.credentials)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = get_user_by_id(db, payload.get("sub", ""))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "账号已禁用")
    if user.token_version != int(payload.get("ver", 0)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效, 请重新登录")

    return user


async def require_owner(user: User = Depends(get_current_user)) -> User:
    """仅 owner 可用(用户管理/系统设置)。"""
    if user.role != "owner":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅管理员可操作")
    return user


def verify_ws_token_payload(db: Session, payload: dict | None) -> str | None:
    """WS 握手专用校验(2026-09-08 T8): decode 之外再对齐 HTTP 层口径。

    校验 is_active + token_version(禁用账号/改密踢人后, 旧 JWT 在剩余有效期
    内不得再连 WS 收通知/行情)。校验通过返回 user_id, 否则 None。
    """
    if not payload or not payload.get("sub"):
        return None
    user = get_user_by_id(db, str(payload["sub"]))
    if not user or not user.is_active:
        return None
    try:
        if user.token_version != int(payload.get("ver", 0)):
            return None
    except (TypeError, ValueError):
        return None
    return user.id


# ── 服务 token(2026-09-07 P1 成熟化: 用户 JWT / 服务 token 双轨) ──────
# 背景: klines/quotes 等纯读行情口挂了用户 JWT, 监控/回填等服务方拿 dashboard
# token 调直接 401。写链路一律保持 get_current_user, 服务 token 永远走不进
# require_owner(role=service ≠ owner → 403), 所以提权面为零。

SERVICE_TOKEN_KEY = "service_token"
SERVICE_TOKEN_HEADER = "X-Service-Token"

_service_token: str | None = None


def get_service_token() -> str:
    """获取服务 token(环境变量优先, 否则 DB 持久化自动生成, 同 jwt_secret 模式)。"""
    global _service_token
    if _service_token:
        return _service_token

    if os.getenv("SIDA_SERVICE_TOKEN"):
        _service_token = os.getenv("SIDA_SERVICE_TOKEN") or ""
        return _service_token

    db = SessionLocal()
    try:
        setting = db.query(AppSettings).filter(AppSettings.key == SERVICE_TOKEN_KEY).first()
        if setting and setting.value:
            tok: str = setting.value
        else:
            tok = secrets.token_hex(32)
            db.add(AppSettings(key=SERVICE_TOKEN_KEY, value=tok, description="服务间只读token(监控/回填/Hub回调, 自动生成)"))
            db.commit()
        _service_token = tok
        return tok
    finally:
        db.close()


class ServicePrincipal:
    """服务调用方身份。注意: 不是 User 行, 只用于只读口的依赖放行。

    router 级 dependencies 的返回值会被丢弃, 故形状无需兼容 User;
    若将来有端点函数直接取它, 按鸭子类型提供 username/role/is_service 即可。
    """

    id = "service"
    username = "service"
    role = "service"
    is_active = True
    is_service = True


async def get_user_or_service(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
):
    """用户 JWT 或服务 token 任一通过即可(仅挂给纯读行情口)。

    顺序: 先试 Bearer 用户 JWT(保持现有行为), 再试 X-Service-Token。
    服务 token 对 require_owner 永远 403, 写链路不受影响。
    """
    if credentials:
        try:
            user = await get_current_user(credentials, db)
            return user
        except HTTPException:
            pass  # Bearer 无效 → 落到服务 token 再试一次, 避免误杀
    svc = (request.headers.get(SERVICE_TOKEN_HEADER) or "").strip()
    if svc and hmac.compare_digest(svc, get_service_token()):
        return ServicePrincipal()
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="未登录",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_service_principal(request: Request) -> ServicePrincipal:
    """仅服务令牌可通过(2026-09-08 风险方案 0.0)。

    用于会返回密钥/内部配置的端点(如 /api/service/forecast-config 下发明文
    api_key)。与 get_user_or_service 的区别: 那个为了兼容旧行为也接受用户 JWT,
    本依赖不接受 —— 明文 api_key 绝不能因为"某人登录了"就下发。
    """
    svc = (request.headers.get(SERVICE_TOKEN_HEADER) or "").strip()
    if svc and hmac.compare_digest(svc, get_service_token()):
        return ServicePrincipal()
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅限服务令牌可通过")


def user_to_dict(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }


# ── API ───────────────────────────────────────────────────────────────

@router.get("/status")
async def auth_status(db: Session = Depends(get_db)):
    """获取认证状态(前端判断是否需要初始化)。"""
    owner = get_or_create_owner(db)
    return {
        "initialized": True,
        "user": user_to_dict(owner),
        "multi_user": True,
    }


@router.post("/login", response_model=TokenResponse)
async def login(data: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """登录(多用户)。带暴力破解限速: 同 IP+用户名 5 次失败锁 10 分钟。"""
    ip = request.client.host if request.client else "unknown"
    from src.core.login_ratelimit import check, fail, success
    locked = check(ip, data.username.strip())
    if locked:
        raise HTTPException(429, locked)

    get_or_create_owner(db)  # 确保 owner 存在(兼容首次部署)
    user = get_user_by_username(db, data.username.strip())
    if not user or not verify_password(data.password, user.password_hash):
        fail(ip, data.username.strip())
        raise HTTPException(401, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账号已禁用")

    # P1-7 (2026-08-23 审计): 登录成功后, 若用户哈希还是旧 SHA-256 或旧 n=2^14
    # scrypt, 透明升级到新参数 (n=2^15) 并落库。下次登录走新参数快路径。
    # 升级失败不阻断登录 (best-effort), 但记 warning 方便排查。
    try:
        if needs_rehash(user.password_hash):
            old_prefix = "scrypt$" if user.password_hash.startswith("scrypt$") else "sha256$"
            user.password_hash = hash_password(data.password)
            db.commit()
            logger.info(
                "[auth] 用户 %s 哈希已透明升级 (旧格式: %s -> 新 scrypt n=%d)",
                user.username, old_prefix, SCRYPT_N_NEW,
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("[auth] 透明哈希升级失败 (不阻断登录): %s", e)

    success(ip, data.username.strip())
    token, expires_at = create_token(user)

    # 操作审计: 登录成功
    # 修复 2026-08-21: audit.py 已用独立 session + best-effort,不再需要这里吞错
    from src.web.api.audit import log_audit
    log_audit(db, user, "login", detail="登录成功", ip=ip)

    return TokenResponse(
        token=token,
        expires_at=expires_at.isoformat(),
        user=user_to_dict(user),
    )


@router.post("/register")
async def register(data: RegisterRequest, request: Request, db: Session = Depends(get_db)):
    """自助注册(member 账号)。

    - 是否开放由 app_settings.allow_register 控制(默认关闭), 关闭时 403
    - 校验: 用户名 2-20 位字母数字; 密码 ≥8 位; 用户名唯一
    """
    from src.web.api.audit import log_audit

    ip = request.client.host if request.client else "unknown"

    # 注册开关: app_settings.allow_register, 默认关闭(无记录或非真值均视为关闭)
    setting = db.query(AppSettings).filter(AppSettings.key == "allow_register").first()
    raw = getattr(setting, "value", None)
    allow_value = str(raw).strip().lower() if raw else ""
    if allow_value not in ("1", "true", "yes", "on"):
        raise HTTPException(403, "注册未开放, 请联系管理员")

    username = data.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{2,20}", username):
        raise HTTPException(400, "用户名需为 2-20 位字母或数字")
    if len(data.password) < 8:
        raise HTTPException(400, "密码长度至少 8 位")
    if get_user_by_username(db, username):
        raise HTTPException(400, "用户名已存在")

    user = create_user(db, username, data.password, "member")  # 默认 is_active=True
    log_audit(db, user, "register", detail=f"注册账号 {username}", ip=ip)
    return {"success": True, "message": "注册成功, 请登录"}


@router.get("/me")
async def get_me(user: User = Depends(get_current_user)):
    """获取当前用户信息。"""
    return {"user": user_to_dict(user)}


@router.post("/change-password")
async def change_password(
    data: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """修改自己的密码, 先校验旧密码, 同时使该用户既有 Token 失效。"""
    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(400, "旧密码不正确")
    if len(data.new_password) < 8:
        raise HTTPException(400, "密码长度至少 8 位")

    user.password_hash = hash_password(data.new_password)
    user.token_version += 1  # 踢掉旧 token
    db.commit()
    # 审计(2026-08-15 评审 B 补覆盖)
    try:
        from src.web.api.audit import log_audit
        log_audit(db, user, "update_password", detail="修改密码", ip="")
    except Exception:
        pass

    return {"message": "密码已更新"}


# ── 用户管理 API(仅 owner) ─────────────────────────────────────────────

class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "member"


@router.get("/users")
async def list_users(owner: User = Depends(require_owner), db: Session = Depends(get_db)):
    """用户列表(仅 owner)。"""
    users = db.query(User).order_by(User.created_at).all()
    return {"users": [user_to_dict(u) for u in users]}


@router.post("/users")
async def create_user_api(
    data: UserCreateRequest,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """创建子账号(仅 owner)。"""
    if len(data.username.strip()) < 2:
        raise HTTPException(400, "用户名长度至少 2 位")
    if len(data.password) < 8:
        raise HTTPException(400, "密码长度至少 8 位")
    if data.role not in ("owner", "member", "guest"):
        raise HTTPException(400, "角色必须是 owner、member 或 guest")
    if get_user_by_username(db, data.username.strip()):
        raise HTTPException(400, "用户名已存在")

    user = create_user(db, data.username.strip(), data.password, data.role)
    return {"user": user_to_dict(user)}


class UserUpdateRequest(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


@router.patch("/users/{user_id}")
async def update_user_api(
    user_id: str,
    data: UserUpdateRequest,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """修改用户(仅 owner): 改密/改角色/禁用。"""
    target = get_user_by_id(db, user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if target.id == owner.id and data.is_active is False:
        raise HTTPException(400, "不能禁用自己")

    if data.password:
        if len(data.password) < 8:
            raise HTTPException(400, "密码长度至少 8 位")
        target.password_hash = hash_password(data.password)
        target.token_version += 1  # 踢掉该用户旧 token
    if data.role and data.role in ("owner", "member", "guest"):
        # P2-10 (2026-09-05 28号审计): 与 create 三值校验对齐, owner 可设 guest
        target.role = data.role
    if data.is_active is not None:
        target.is_active = data.is_active
    db.commit()

    return {"user": user_to_dict(target)}


@router.delete("/users/{user_id}")
async def delete_user_api(
    user_id: str,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """删除用户(仅 owner)。不能删自己。"""
    target = get_user_by_id(db, user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if target.id == owner.id:
        raise HTTPException(400, "不能删除自己")
    if target.role == "owner":
        raise HTTPException(400, "不能删除其他管理员")

    db.delete(target)
    db.commit()
    return {"message": "用户已删除"}
