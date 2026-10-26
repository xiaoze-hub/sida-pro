"""邮箱验证码 API (2026-09-16)

- POST /api/auth/send-code: 发送 6 位数字验证码(5 分钟过期, 每邮箱每分钟 1 次)
- POST /api/auth/login-by-email: 邮箱 + 验证码登录
- 存储: 进程内 dict + threading.Lock(不依赖 Redis)
- SMTP 未配置时走开发模式: 验证码打印到日志
"""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.web.database import get_db
from src.web.models import User

router = APIRouter()
logger = logging.getLogger(__name__)

# 验证码配置
CODE_TTL_SECONDS = 300  # 5 分钟
SEND_COOLDOWN_SECONDS = 60  # 每邮箱每分钟 1 次
PURPOSES = ("register", "login", "reset")  # 2026-10-23: reset = 忘记密码

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


@dataclass
class EmailCode:
    code: str
    email: str
    purpose: str
    created_at: float
    used: bool = False


# 进程内存储: key = f"{email}:{purpose}"
_store: dict[str, EmailCode] = {}
_lock = threading.Lock()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _store_key(email: str, purpose: str) -> str:
    return f"{email}:{purpose}"


def _purge_expired_locked() -> None:
    """调用方需持有 _lock。清理过期/已用验证码, 防无限膨胀。"""
    now = time.time()
    dead = [
        k
        for k, v in _store.items()
        if v.used or (now - v.created_at) > CODE_TTL_SECONDS
    ]
    for k in dead:
        _store.pop(k, None)


def generate_code() -> str:
    return f"{random.randint(0, 999999):06d}"


def create_and_send_code(email: str, purpose: str) -> None:
    """生成验证码、写入存储、发送邮件。调用方已完成限流检查。"""
    code = generate_code()
    email_n = _normalize_email(email)
    with _lock:
        _purge_expired_locked()
        _store[_store_key(email_n, purpose)] = EmailCode(
            code=code,
            email=email_n,
            purpose=purpose,
            created_at=time.time(),
            used=False,
        )

    purpose_label = "注册" if purpose == "register" else "登录"
    subject = f"【数智分析 SIDA】{purpose_label}验证码"
    body = (
        f"您的{purpose_label}验证码是: {code}\n"
        f"有效期 {CODE_TTL_SECONDS // 60} 分钟。如非本人操作请忽略本邮件。"
    )
    ok = _send_email(email_n, subject, body)
    if not ok:
        # 发送失败时移除刚写入的验证码, 避免"以为发了其实没发"
        with _lock:
            _store.pop(_store_key(email_n, purpose), None)
        if not all([os.getenv("SMTP_HOST"), os.getenv("SMTP_USER"), os.getenv("SMTP_PASS")]):
            # 配置问题 → 说实话, 别让用户"稍后重试"重试到天荒地老
            raise HTTPException(503, "邮件服务未配置, 请把邮箱发给管理员手工开通")
        raise HTTPException(500, "验证码发送失败, 请稍后重试")


def verify_code(email: str, purpose: str, code: str) -> bool:
    """校验验证码: 未过期 + 未使用 + 匹配 → 标记 used 并返回 True。"""
    email_n = _normalize_email(email)
    code = (code or "").strip()
    if not code:
        return False
    with _lock:
        item = _store.get(_store_key(email_n, purpose))
        if not item:
            return False
        if item.used:
            return False
        if (time.time() - item.created_at) > CODE_TTL_SECONDS:
            _store.pop(_store_key(email_n, purpose), None)
            return False
        if not hmac_compare(item.code, code):
            return False
        item.used = True
        return True


def hmac_compare(a: str, b: str) -> bool:
    import hmac as _hmac

    return _hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def check_send_cooldown(email: str, purpose: str) -> Optional[str]:
    """检查发送冷却。返回剩余秒数描述; 无限制返回 None。"""
    email_n = _normalize_email(email)
    with _lock:
        item = _store.get(_store_key(email_n, purpose))
        if item and not item.used:
            elapsed = time.time() - item.created_at
            if elapsed < SEND_COOLDOWN_SECONDS:
                remain = int(SEND_COOLDOWN_SECONDS - elapsed) + 1
                return f"发送过于频繁, 请 {remain} 秒后再试"
    return None


def _dev_email_log_only() -> bool:
    """本地开发用: 没配 SMTP 时把验证码打到日志并**假装**发送成功(默认关闭)。

    生产**不得**开启 —— 否则又回到"用户以为发了, 其实只写日志"的老问题。
    """
    return os.getenv("EMAIL_DEV_LOG_ONLY", "").strip().lower() in ("1", "true", "yes", "on")


def _send_email(to: str, subject: str, body: str) -> bool:
    import smtplib
    from email.mime.text import MIMEText

    host = os.getenv("SMTP_HOST")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    pwd = os.getenv("SMTP_PASS")
    if not all([host, user, pwd]):
        # 2026-09-19 修: 原来这里**无条件**返回 True("开发模式") —— 生产上没配 SMTP 时会
        # 静默假装发送成功(验证码只写进日志), 用户看到"已发送"却永远收不到, 只会以为是自己邮箱问题。
        # 现在: 只有**显式开了** EMAIL_DEV_LOG_ONLY(本地开发)才假装成功; 否则如实返回失败,
        # 让端点给出明确文案(配置问题不该说成"稍后重试")。
        if _dev_email_log_only():
            logger.info("[DEV] 验证码邮件 to=%s: %s", to, body)
            return True
        logger.error(
            "[email_verify] SMTP 未配置(SMTP_HOST/SMTP_USER/SMTP_PASS), 无法发送验证码邮件 to=%s —— "
            "生产环境必须配置邮件服务, 否则邮箱注册/验证码登录不可用", to,
        )
        return False

    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to
        use_ssl = os.getenv("SMTP_SSL", "").strip().lower() in ("1", "true", "yes", "on")
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.ehlo()
            if server.has_extn("starttls"):
                server.starttls()
                server.ehlo()
        try:
            server.login(user, pwd)
            server.sendmail(user, [to], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
        logger.info("[email_verify] 验证码邮件已发送 to=%s", to)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[email_verify] 邮件发送失败 to=%s: %s", to, e)
        return False


# ── 请求模型 ──────────────────────────────────────────────────────────


class SendCodeRequest(BaseModel):
    email: str
    purpose: str = "register"  # register | login


class LoginByEmailRequest(BaseModel):
    email: str
    code: str


# ── API ───────────────────────────────────────────────────────────────


@router.post("/send-code")
async def send_code(data: SendCodeRequest, request: Request):
    """发送邮箱验证码。purpose: register | login。"""
    email = _normalize_email(data.email)
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(400, "邮箱格式不正确")
    purpose = (data.purpose or "").strip().lower()
    if purpose not in PURPOSES:
        raise HTTPException(400, "purpose 必须是 register / login / reset")

    locked_msg = check_send_cooldown(email, purpose)
    if locked_msg:
        raise HTTPException(429, locked_msg)

    create_and_send_code(email, purpose)
    return {"sent": True, "expire_in": CODE_TTL_SECONDS}


@router.post("/login-by-email")
async def login_by_email(
    data: LoginByEmailRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """邮箱验证码登录。验证通过后签发 JWT(格式同 /login)。用户不存在 404。"""
    # 延迟导入避免循环依赖(auth 模块在 register 中反向引用本模块)
    from src.web.api.auth import (
        create_token,
        get_user_by_email,
        normalize_email,
        set_auth_cookie,
        user_to_dict,
    )

    email = normalize_email(data.email)
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(400, "邮箱格式不正确")

    code = (data.code or "").strip()
    if not code:
        raise HTTPException(400, "请输入验证码")

    if not verify_code(email, "login", code):
        raise HTTPException(400, "验证码错误或已过期")

    user = get_user_by_email(db, email)
    if not user:
        raise HTTPException(404, "该邮箱未注册")
    if not user.is_active:
        raise HTTPException(403, "账号已禁用")

    ip = request.client.host if request.client else "unknown"
    tok = create_token(user)
    token, expires_at = tok

    # 设备限制: 与密码登录口径一致(session_id = JWT jti, 2026-10-23 修复)
    try:
        from src.core.permissions import record_session

        record_session(
            db, user, session_id=tok.jti, expires_at=expires_at,
            ip=ip, user_agent=request.headers.get("user-agent", ""),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[email_verify] record_session 失败(不阻断登录): %s", e)

    try:
        from src.web.api.audit import log_audit

        log_audit(db, user, "login", detail="邮箱验证码登录", ip=ip)
    except Exception:
        pass

    api_key_prefix: Optional[str] = None
    try:
        from src.web.models import SkillApiKey

        key_row = (
            db.query(SkillApiKey)
            .filter(SkillApiKey.user_id == user.id, SkillApiKey.status == "active")
            .order_by(SkillApiKey.created_at.desc())
            .first()
        )
        if key_row:
            api_key_prefix = key_row.key_prefix
    except Exception as e:  # noqa: BLE001
        logger.warning("[email_verify] 查询用户 API key 前缀失败(不阻断登录): %s", e)

    # P0(2026-09-18): 与密码登录一致, 同步下发 httpOnly JWT Cookie
    set_auth_cookie(response, request, token)

    return {
        "token": token,
        "expires_at": expires_at.isoformat(),
        "user": user_to_dict(user),
        "api_key_prefix": api_key_prefix,
    }


# ── 忘记密码(2026-10-23) ──────────────────────────────────────────────
# 复用 register/login 的验证码通道(purpose="reset"), 不新增短信/邮件实现。
# 安全约束: 不返回 token(不自动登录) —— 重置后必须用新密码重新登录,
# 同时 token_version += 1 使所有已签发 JWT 立即失效(与改密口径一致)。


class ResetPasswordByEmailRequest(BaseModel):
    email: str
    code: str
    new_password: str


PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 128  # 上限防超长输入拖慢 scrypt


@router.post("/reset-password-by-email")
async def reset_password_by_email(
    data: ResetPasswordByEmailRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """邮箱验证码重置密码。成功不自动登录, 旧会话全部失效。"""
    from src.web.api.auth import get_user_by_email, hash_password

    email = _normalize_email(data.email)
    if not EMAIL_RE.fullmatch(email):
        raise HTTPException(400, "邮箱格式不正确")

    code = (data.code or "").strip()
    if not code:
        raise HTTPException(400, "请输入验证码")

    new_password = data.new_password or ""
    if len(new_password) < PASSWORD_MIN_LEN:
        raise HTTPException(400, f"新密码至少 {PASSWORD_MIN_LEN} 位")
    if len(new_password) > PASSWORD_MAX_LEN:
        raise HTTPException(400, f"新密码不能超过 {PASSWORD_MAX_LEN} 位")

    # 先校验验证码(消费一次性码), 再判断账号 —— 避免未持码的探测者枚举邮箱。
    if not verify_code(email, "reset", code):
        raise HTTPException(400, "验证码错误或已过期")

    user = get_user_by_email(db, email)
    if not user:
        raise HTTPException(404, "该邮箱未注册")
    if not user.is_active:
        raise HTTPException(403, "账号已禁用")

    user.password_hash = hash_password(new_password)
    user.token_version = (user.token_version or 0) + 1
    # 会话行同步清理: token_version 已能让旧 JWT 失效, 这里删行是为了
    # 「在线设备」列表立刻干净, 不给用户看到已失效的僵尸设备。
    try:
        from src.core.permissions import mark_session_revoked
        from src.db.models import UserSession

        for row in db.query(UserSession).filter(UserSession.user_id == user.id).all():
            mark_session_revoked(db, row.session_id, row.expires_at)
            db.delete(row)
    except Exception as e:  # noqa: BLE001 - 清理失败不阻断重置
        logger.warning("[email_verify] 重置密码后清理会话失败(不阻断): %s", e)

    db.commit()

    ip = request.client.host if request.client else ""
    try:
        from src.web.api.audit import log_audit

        log_audit(db, user, "reset_password_by_email", detail="邮箱验证码重置密码", ip=ip)
    except Exception:  # noqa: BLE001 - 审计失败不阻断
        pass

    return {
        "success": True,
        "message": "密码已重置, 请用新密码登录",
        # 显式声明: 不签发 token, 前端必须切回密码登录
        "auto_login": False,
    }
