"""用户权限体系(2026-09-15, 对标 DeepSeek 开放平台前置)。

四档: guest / member / pro / owner。
- 普通: 行情、热力图、持仓(自选≤10, 预警≤3)
- 锁死: 机会、三指标、暗盘、L2 → 各 3 次/天试用, 用完 403+Pro 引导
- 设备: 同账号同时在线 ≤2, 超了踢最早
- 设置: 个人中心=self, 系统设置=owner

矩阵见 docs/permission-matrix.md。
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timezone
from typing import Callable

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from src.db.models import User

logger = logging.getLogger(__name__)

# 角色
ROLE_GUEST = "guest"
ROLE_MEMBER = "member"
ROLE_PRO = "pro"
ROLE_OWNER = "owner"  # 即 admin

# 权限点
PERM_VIEW_QUOTE = "view_quote"
PERM_VIEW_HEATMAP = "view_heatmap"
PERM_EDIT_PORTFOLIO = "edit_portfolio"
PERM_VIEW_OPPORTUNITIES = "view_opportunities"
PERM_VIEW_FORECAST = "view_forecast"
PERM_VIEW_L2 = "view_l2"
PERM_VIEW_DARK = "view_dark"
PERM_MANAGE_USERS = "manage_users"
PERM_MANAGE_SYSTEM = "manage_system"

# 普通账号锁死但可试用的功能(3 次/天)
TRIAL_FEATURES = {
    PERM_VIEW_OPPORTUNITIES: "机会",
    PERM_VIEW_FORECAST: "预测",
    PERM_VIEW_L2: "L2资金",
    PERM_VIEW_DARK: "暗盘资金",
}
TRIAL_DAILY_LIMIT = 3

# 角色 → 权限点集合
_ROLE_PERMS: dict[str, set[str]] = {
    ROLE_GUEST: set(),
    ROLE_MEMBER: {
        PERM_VIEW_QUOTE,
        PERM_VIEW_HEATMAP,
        PERM_EDIT_PORTFOLIO,
    },
    ROLE_PRO: {
        PERM_VIEW_QUOTE,
        PERM_VIEW_HEATMAP,
        PERM_EDIT_PORTFOLIO,
        PERM_VIEW_OPPORTUNITIES,
        PERM_VIEW_FORECAST,
        PERM_VIEW_L2,
        PERM_VIEW_DARK,
    },
    ROLE_OWNER: {
        PERM_VIEW_QUOTE,
        PERM_VIEW_HEATMAP,
        PERM_EDIT_PORTFOLIO,
        PERM_VIEW_OPPORTUNITIES,
        PERM_VIEW_FORECAST,
        PERM_VIEW_L2,
        PERM_VIEW_DARK,
        PERM_MANAGE_USERS,
        PERM_MANAGE_SYSTEM,
    },
}

# 普通账号配额
MEMBER_WATCHLIST_MAX = 10
MEMBER_ALERT_MAX = 3


def normalize_role(role: str | None) -> str:
    """DB role → 四档。owner=admin; 未知角色按 member。"""
    r = (role or "").strip().lower()
    if r in ("owner", "admin"):
        return ROLE_OWNER
    if r == "pro":
        return ROLE_PRO
    if r == "guest":
        return ROLE_GUEST
    return ROLE_MEMBER


def has_perm(user: User | None, perm: str) -> bool:
    """是否有权限(不含试用)。guest=无。"""
    if user is None:
        return False
    role = normalize_role(getattr(user, "role", None))
    return perm in _ROLE_PERMS.get(role, set())


def enforce_perm(user: User, perm: str, db: Session | None = None) -> None:
    """进程内校验权限; member 试用功能计数。无权限/试用尽则抛 HTTPException。

    给路由函数体内部调用, 避开 FastAPI Depends 的循环 import。
    """
    if user is None:
        raise HTTPException(401, "未登录")
    role = normalize_role(user.role)
    if perm in _ROLE_PERMS.get(role, set()):
        return
    if perm in TRIAL_FEATURES and role == ROLE_MEMBER and db is not None:
        used = _trial_used(db, user.id, perm)
        if used < TRIAL_DAILY_LIMIT:
            _trial_incr(db, user.id, perm)
            return
        raise HTTPException(
            403,
            detail={
                "message": f"「{TRIAL_FEATURES[perm]}」试用次数已用完({TRIAL_DAILY_LIMIT}次/天)",
                "pro_guide": True,
                "feature": perm,
            },
        )
    raise HTTPException(403, f"无权限: {perm}")


def require_perm(perm: str) -> Callable:
    """FastAPI 依赖版(兼容既有调用); 内部走 enforce_perm。

    懒 import auth — 避开 `src/web/api/__init__.py` 循环。
    """
    from fastapi import Request

    async def _dep(request: Request) -> User:
        from fastapi.security import HTTPBearer

        from src.web.api.auth import get_current_user
        from src.web.database import SessionLocal

        security = HTTPBearer(auto_error=False)
        creds = await security(request)
        db = SessionLocal()
        try:
            user = await get_current_user(credentials=creds, db=db)
            enforce_perm(user, perm, db)
            return user
        finally:
            db.close()

    return _dep


def _today() -> str:
    return date.today().isoformat()


def _trial_used(db: Session, user_id: str, feature: str) -> int:
    """当日试用次数(进程内存 + 可选 Redis; 先内存兜底)。"""
    key = f"{user_id}:{feature}:{_today()}"
    with _TRIAL_LOCK:
        return _TRIAL_MEM.get(key, 0)


def _trial_incr(db: Session, user_id: str, feature: str) -> None:
    key = f"{user_id}:{feature}:{_today()}"
    with _TRIAL_LOCK:
        _TRIAL_MEM[key] = _TRIAL_MEM.get(key, 0) + 1
    # 落库审计(可选, 失败不影响)
    try:
        from src.web.models import AuditLog

        db.add(AuditLog(
            user_id=user_id,
            username="",
            action=f"trial_{feature}",
            detail=f"count={_TRIAL_MEM.get(key, 0)}",
        ))
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("trial audit 落库失败: %r", e)


# 试用计数(进程内存; 多 worker 各一份, 日限 3 影响可接受)
import threading as _threading  # noqa: E402

_TRIAL_LOCK = _threading.Lock()
_TRIAL_MEM: dict[str, int] = {}


def check_watchlist_quota(db: Session, user: User) -> None:
    """普通账号自选≤10。"""
    role = normalize_role(user.role)
    if role in (ROLE_PRO, ROLE_OWNER):
        return
    from src.web.models import Stock

    n = db.query(Stock).filter(Stock.user_id == user.id).count()
    if n >= MEMBER_WATCHLIST_MAX:
        raise HTTPException(403, f"普通账号自选上限 {MEMBER_WATCHLIST_MAX} 只, 升级 Pro 可无限")


def check_alert_quota(db: Session, user: User) -> None:
    """普通账号预警≤3。"""
    role = normalize_role(user.role)
    if role in (ROLE_PRO, ROLE_OWNER):
        return
    try:
        from src.web.models import PriceAlertRule

        n = db.query(PriceAlertRule).filter(PriceAlertRule.user_id == user.id).count()
        if n >= MEMBER_ALERT_MAX:
            raise HTTPException(403, f"普通账号预警上限 {MEMBER_ALERT_MAX} 条, 升级 Pro 可更多")
    except ImportError:
        pass


# 设备限制(同账号同时在线 ≤2)
MAX_SESSIONS_PER_USER = 2


def enforce_device_limit(db: Session, user: User, session_id: str) -> None:
    """登录时: 超过 2 台踢最早。"""
    try:
        from src.web.models import UserSession  # 若无此表则跳过

        rows = (
            db.query(UserSession)
            .filter(UserSession.user_id == user.id, UserSession.expires_at > datetime.now(timezone.utc))
            .order_by(UserSession.last_seen.asc())
            .all()
        )
        if len(rows) >= MAX_SESSIONS_PER_USER:
            # 踢最早的(除了本次)
            to_kick = [r for r in rows if r.session_id != session_id]
            if to_kick:
                oldest = to_kick[0]
                db.delete(oldest)
                db.commit()
                logger.info("设备限制: 踢掉 %s 的会话 %s", user.username, oldest.session_id[:8])
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.debug("设备限制检查失败: %r", e)
