"""SIDA 用户权限体系(2026-09-15 四档重构, 合并原 RBAC)。

四档: guest / member / pro / owner(admin)。
- member: 行情、热力图、持仓(自选≤10, 预警≤3); 锁死功能 3 次/天试用
- pro: 全功能(不含系统管理)
- owner: 全功能 + 系统设置

兼容原 RBAC 权限点(view_*/manage_*/edit_*), 供中间件/前端导航过滤继续使用。
矩阵见 docs/permission-matrix.md。
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone
from typing import Callable

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
# 原 RBAC 权限点(2026-08-15, 中间件/前端导航继续用)
# ════════════════════════════════════════════════════════════════════
VIEW_PERMISSIONS = frozenset({
    "view_dashboard",
    "view_quotes",
    "view_forecast",
    "view_reports",
    "view_opportunities",
})

MANAGE_PERMISSIONS = frozenset({
    "manage_datasources",
    "manage_settings",
    "manage_ai_services",
    "manage_users",
    "manage_agents",
    "manage_strategies",
    "manage_shadow",
    "manage_paper_trading",
})

MEMBER_EXTRA_PERMISSIONS = frozenset({
    "edit_watchlist",
    "edit_portfolio",
    "run_prediction",
    "use_chat",
    "upload_files",
})

# 新增权限点(四档体系, 2026-09-15)
_VIEW_PERMISSIONS_NEW = frozenset({
    "view_heatmap",
    "view_l2",
    "view_dark",
})
_MANAGE_PERMISSIONS_NEW = frozenset({
    "manage_system",
})

# 全量权限点(含新增)
ALL_PERMISSIONS = (
    VIEW_PERMISSIONS
    | MANAGE_PERMISSIONS
    | MEMBER_EXTRA_PERMISSIONS
    | _VIEW_PERMISSIONS_NEW
    | _MANAGE_PERMISSIONS_NEW
)

# guest(demo) 附加限制常量
GUEST_STRATEGY: dict[str, int] = {
    "watchlist_limit": 1,
    "get_hourly_limit": 20,
}

# ════════════════════════════════════════════════════════════════════
# 四档角色 + 新增权限点(2026-09-15)
# ════════════════════════════════════════════════════════════════════
ROLE_GUEST = "guest"
ROLE_MEMBER = "member"
ROLE_PRO = "pro"
ROLE_OWNER = "owner"  # 即 admin

# 权限点常量别名
PERM_VIEW_QUOTE = "view_quotes"
PERM_VIEW_HEATMAP = "view_heatmap"
PERM_VIEW_OPPORTUNITIES = "view_opportunities"
PERM_VIEW_FORECAST = "view_forecast"
PERM_VIEW_L2 = "view_l2"
PERM_VIEW_DARK = "view_dark"
PERM_EDIT_PORTFOLIO = "edit_portfolio"
PERM_MANAGE_USERS = "manage_users"
PERM_MANAGE_SYSTEM = "manage_system"

# member 锁死但可试用的功能(3 次/天)
TRIAL_FEATURES = {
    PERM_VIEW_OPPORTUNITIES: "机会",
    PERM_VIEW_FORECAST: "数智决策",
    PERM_VIEW_L2: "L2资金",
    PERM_VIEW_DARK: "暗盘资金",
}
TRIAL_DAILY_LIMIT = 3

# member 基础权限(不含试用)
_MEMBER_BASE = (
    VIEW_PERMISSIONS
    - set(TRIAL_FEATURES)
    | {"view_heatmap"}
    | MEMBER_EXTRA_PERMISSIONS
)

# pro = member 基础 + 全部试用功能
_PRO_PERMS = _MEMBER_BASE | set(TRIAL_FEATURES)

# owner = 全量
_OWNER_PERMS = set(ALL_PERMISSIONS)

# 角色 → 权限点集合(统一入口, 中间件/前端都从这取)
# 2026-09-15 四档: guest 无权限(未登录只看首页, 点功能弹注册)
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    ROLE_GUEST: frozenset(),  # guest 无权限
    ROLE_MEMBER: frozenset(_MEMBER_BASE),
    ROLE_PRO: frozenset(_PRO_PERMS),
    ROLE_OWNER: frozenset(_OWNER_PERMS),
}

# 向后兼容别名
_ROLE_PERMS = {k: set(v) for k, v in ROLE_PERMISSIONS.items()}

# 普通账号配额
MEMBER_WATCHLIST_MAX = 10
MEMBER_ALERT_MAX = 3

# 设备限制
MAX_SESSIONS_PER_USER = 2


# ════════════════════════════════════════════════════════════════════
# 核心函数
# ════════════════════════════════════════════════════════════════════
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


def get_role_permissions(role: str | None) -> set[str]:
    """返回角色对应的权限点集合; 未知角色返回空集(最严)。

    兼容原 RBAC 调用; 四档角色也走这里。
    """
    if not role:
        return set()
    # 先查四档, 再查原 RBAC(兼容)
    r = role.strip().lower()
    if r in ROLE_PERMISSIONS:
        return set(ROLE_PERMISSIONS[r])
    if r == "admin":
        return set(ROLE_PERMISSIONS[ROLE_OWNER])
    return set(ROLE_PERMISSIONS.get(r, frozenset()))


def has_permission(role: str | None, perm: str) -> bool:
    """判断角色是否拥有某权限点(原 RBAC 接口)。"""
    return perm in get_role_permissions(role)


def has_perm(user, perm: str) -> bool:
    """是否有权限(不含试用)。接受 User 对象或 role 字符串。"""
    if user is None:
        return False
    role = getattr(user, "role", user) if not isinstance(user, str) else user
    return perm in get_role_permissions(role)


def enforce_perm(user, perm: str, db: Session | None = None) -> None:
    """进程内校验权限; member 试用功能计数。无权限/试用尽则抛 HTTPException。

    给路由函数体内部调用, 避开 FastAPI Depends 的循环 import。
    """
    if user is None:
        raise HTTPException(401, "未登录")
    role = normalize_role(getattr(user, "role", None))
    if perm in get_role_permissions(role):
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
    """FastAPI 依赖版; 内部走 enforce_perm。"""
    from fastapi import Request

    async def _dep(request: Request):
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


# ════════════════════════════════════════════════════════════════════
# 试用计数(进程内存; 多 worker 各一份, 日限 3 影响可接受)
# ════════════════════════════════════════════════════════════════════
_TRIAL_LOCK = threading.Lock()
_TRIAL_MEM: dict[str, int] = {}


def _today() -> str:
    return date.today().isoformat()


def _trial_key(user_id: str, feature: str) -> str:
    return f"{user_id}:{feature}:{_today()}"


def _trial_used(db: Session, user_id: str, feature: str) -> int:
    with _TRIAL_LOCK:
        return _TRIAL_MEM.get(_trial_key(user_id, feature), 0)


def _trial_incr(db: Session, user_id: str, feature: str) -> None:
    key = _trial_key(user_id, feature)
    with _TRIAL_LOCK:
        _TRIAL_MEM[key] = _TRIAL_MEM.get(key, 0) + 1
        count = _TRIAL_MEM[key]
    try:
        from src.web.models import AuditLog

        db.add(AuditLog(
            user_id=user_id,
            username="",
            action=f"trial_{feature}",
            detail=f"count={count}",
        ))
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.debug("trial audit 落库失败: %r", e)


def get_trial_remaining(db: Session, user_id: str, feature: str) -> int:
    """查询当日剩余试用次数(前端展示用)。"""
    used = _trial_used(db, user_id, feature)
    return max(0, TRIAL_DAILY_LIMIT - used)


# ════════════════════════════════════════════════════════════════════
# 配额检查
# ════════════════════════════════════════════════════════════════════
def check_watchlist_quota(db: Session, user) -> None:
    """普通账号自选≤10。"""
    role = normalize_role(getattr(user, "role", None))
    if role in (ROLE_PRO, ROLE_OWNER):
        return
    from src.web.models import Stock

    n = db.query(Stock).filter(Stock.user_id == user.id).count()
    if n >= MEMBER_WATCHLIST_MAX:
        raise HTTPException(403, f"普通账号自选上限 {MEMBER_WATCHLIST_MAX} 只, 升级 Pro 可无限")


def check_alert_quota(db: Session, user) -> None:
    """普通账号预警≤3。"""
    role = normalize_role(getattr(user, "role", None))
    if role in (ROLE_PRO, ROLE_OWNER):
        return
    try:
        from src.web.models import PriceAlertRule

        n = db.query(PriceAlertRule).filter(PriceAlertRule.user_id == user.id).count()
        if n >= MEMBER_ALERT_MAX:
            raise HTTPException(403, f"普通账号预警上限 {MEMBER_ALERT_MAX} 条, 升级 Pro 可更多")
    except ImportError:
        pass


# ════════════════════════════════════════════════════════════════════
# 设备限制(同账号同时在线 ≤2)
# ════════════════════════════════════════════════════════════════════
def enforce_device_limit(db: Session, user, session_id: str) -> None:
    """登录时: 超过 2 台踢最早。"""
    try:
        from src.web.models import UserSession

        rows = (
            db.query(UserSession)
            .filter(UserSession.user_id == user.id, UserSession.expires_at > datetime.now(timezone.utc))
            .order_by(UserSession.last_seen.asc())
            .all()
        )
        if len(rows) >= MAX_SESSIONS_PER_USER:
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


def record_session(db: Session, user, session_id: str, expires_at: datetime) -> None:
    """登录成功后记录会话(设备限制用)。"""
    try:
        from src.web.models import UserSession

        existing = db.query(UserSession).filter(UserSession.session_id == session_id).first()
        if existing:
            existing.last_seen = datetime.now(timezone.utc)
            existing.expires_at = expires_at
        else:
            db.add(UserSession(
                session_id=session_id,
                user_id=user.id,
                expires_at=expires_at,
                last_seen=datetime.now(timezone.utc),
            ))
        db.commit()
        enforce_device_limit(db, user, session_id)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.debug("记录会话失败: %r", e)


# ════════════════════════════════════════════════════════════════════
# 权限点中文标签(前端「模块权限」设置 UI 用)
# ════════════════════════════════════════════════════════════════════
PERMISSION_LABELS: dict[str, tuple[str, str]] = {
    "view_dashboard": ("首页", "浏览"),
    "view_quotes": ("行情", "浏览"),
    "view_heatmap": ("板块热力", "浏览"),
    "view_forecast": ("数智决策", "浏览"),
    "view_reports": ("报告", "浏览"),
    "view_opportunities": ("机会", "浏览"),
    "view_l2": ("L2资金", "浏览"),
    "view_dark": ("暗盘资金", "浏览"),
    "edit_watchlist": ("自选管理", "操作"),
    "edit_portfolio": ("持仓管理", "操作"),
    "run_prediction": ("发起预测", "操作"),
    "use_chat": ("AI 对话", "操作"),
    "upload_files": ("文件上传", "操作"),
    "manage_datasources": ("数据源", "管理"),
    "manage_settings": ("系统设置", "管理"),
    "manage_ai_services": ("AI 服务商", "管理"),
    "manage_users": ("用户管理", "管理"),
    "manage_agents": ("Agent 管理", "管理"),
    "manage_strategies": ("策略库", "管理"),
    "manage_shadow": ("影子账户", "管理"),
    "manage_paper_trading": ("模拟盘", "管理"),
    "manage_system": ("系统管理", "管理"),
}
