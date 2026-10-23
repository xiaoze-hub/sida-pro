"""SIDA 用户权限体系(2026-09-15 四档重构, 合并原 RBAC)。

四档: guest / member / pro / owner(admin)。
- member: 行情、热力图、持仓(自选/预警上限**运行时可调**, 见 free_tier); 少数功能按"免费档"配置试用
- pro: 全功能(不含系统管理) —— **数智决策三指标(机构活跃度/GS/L2主力净流入 TQ口径)、集合竞价池
  属 pro 专属**, 默认不在免费层级
- owner: 全功能 + 系统设置(含「免费档」面板, 可运行时调整 member 能试用什么、每天几次)

免费档(试用功能/日限/自选上限/skill 档位覆盖)由 `src/core/free_tier.py` 从 app_settings 读,
owner 通过 `GET/PUT /api/admin/free-tier` 调整, 30s 内热生效 —— 不再需要改代码发版。

兼容原 RBAC 权限点(view_*/manage_*/edit_*), 供中间件/前端导航过滤继续使用。
矩阵见 docs/permission-matrix.md。
"""
from __future__ import annotations

import importlib
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
    "view_auction",  # 集合竞价池(2026-09-18): pro 专属, 见 PRO_ONLY_PERMS
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
#: 集合竞价池(9:25 竞价数据 + 竞价异动池) —— 2026-09-18 起 pro 专属
PERM_VIEW_AUCTION = "view_auction"
PERM_MANAGE_SYSTEM = "manage_system"

# ── pro 专属功能(2026-09-18 重构) ────────────────────────────────
# 这些功能**不再属于 member 基础权限**; member 想要访问只能靠"免费档"把它们临时列进试用。
#
# 2026-09-18 老板拍板: `view_forecast`(数智决策三指标: 机构活跃度 + GS + L2主力净流入 TQ口径)
# 与 `view_auction`(集合竞价池, 9:25 竞价数据) **一律 pro 档, 不在免费层级** ——
# HTTP API / 外部 skill 调用都一样。默认免费档只剩 机会/L2资金/暗盘资金。
PRO_ONLY_PERMS: frozenset[str] = frozenset({
    PERM_VIEW_OPPORTUNITIES,
    PERM_VIEW_FORECAST,
    PERM_VIEW_L2,
    PERM_VIEW_DARK,
    PERM_VIEW_AUCTION,
})

#: pro 专属功能的中文名(免费档面板 / 403 提示用)
PRO_ONLY_LABELS: dict[str, str] = {
    PERM_VIEW_OPPORTUNITIES: "机会",
    PERM_VIEW_FORECAST: "数智决策",
    PERM_VIEW_L2: "L2资金",
    PERM_VIEW_DARK: "暗盘资金",
    PERM_VIEW_AUCTION: "集合竞价池",
}

# 兼容旧引用: 默认免费档(实际判定走 free_tier.trial_features(), 可运行时调整)
TRIAL_FEATURES = dict(importlib.import_module("src.core.free_tier").DEFAULT_TRIAL_FEATURES)
TRIAL_DAILY_LIMIT = importlib.import_module("src.core.free_tier").DEFAULT_TRIAL_DAILY_LIMIT


def effective_trial_features(db=None) -> dict[str, str]:
    """当前真正生效的免费档功能(运行时配置优先, 读不到用默认)。"""
    try:
        from src.core import free_tier

        return free_tier.trial_features(db)
    except Exception:  # noqa: BLE001 —— 配置层故障不该让权限判定崩, 回落默认
        return dict(TRIAL_FEATURES)


def effective_trial_limit(db=None) -> int:
    try:
        from src.core import free_tier

        return free_tier.trial_daily_limit(db)
    except Exception:  # noqa: BLE001
        return TRIAL_DAILY_LIMIT


def effective_max_sessions(db=None) -> int:
    """同账号同时在线设备数上限(运行时配置优先, 读不到用默认 2)。"""
    try:
        from src.core import free_tier

        return free_tier.max_sessions_per_user(db)
    except Exception:  # noqa: BLE001 —— 配置层故障不该让登录失败, 回落默认
        return MAX_SESSIONS_PER_USER


def effective_guest_strategy(db=None) -> dict[str, int]:
    """游客限流策略(运行时配置优先, 读不到用 GUEST_STRATEGY 常量)。"""
    try:
        from src.core import free_tier

        return free_tier.guest_strategy(db)
    except Exception:  # noqa: BLE001
        return dict(GUEST_STRATEGY)


# member 基础权限 = 通用浏览权 - pro 专属 + 热力图 + member 操作权
_MEMBER_BASE = (
    VIEW_PERMISSIONS
    | _VIEW_PERMISSIONS_NEW
    | {"view_heatmap"}
    | MEMBER_EXTRA_PERMISSIONS
) - set(PRO_ONLY_PERMS)

# pro = member 基础 + 全部 pro 专属功能
_PRO_PERMS = _MEMBER_BASE | set(PRO_ONLY_PERMS)

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
    trial = effective_trial_features(db)
    if perm in trial and role == ROLE_MEMBER:
        limit = effective_trial_limit(db)
        label = trial.get(perm) or PRO_ONLY_LABELS.get(perm, perm)
        if db is None:
            # 无 db 上下文(个别内部调用): 不计数直接放行 —— 与旧行为一致(旧实现也要求 db 非空)
            return
        used = _trial_used(db, user.id, perm)
        if limit > 0 and used < limit:
            _trial_incr(db, user.id, perm)
            return
        raise HTTPException(
            403,
            detail={
                "message": f"「{label}」试用次数已用完({limit}次/天)",
                "pro_guide": True,
                "feature": perm,
            },
        )
    # pro 专属功能但**不在**免费档 → 明确引导升级(区别于"压根没这个权限点")
    if perm in PRO_ONLY_PERMS:
        raise HTTPException(
            403,
            detail={
                "message": f"「{PRO_ONLY_LABELS.get(perm, perm)}」为 Pro 专属功能, 升级后可用",
                "pro_guide": True,
                "feature": perm,
                "pro_only": True,
            },
        )
    raise HTTPException(403, f"无权限: {perm}")


# 2026-09-18(B4.1 棘轮): 原 `require_perm`(FastAPI 依赖版)已删 —— 全仓零引用,
# 且它把 FastAPI/HTTPBearer + src.web.api.auth 拖进 core 层。core 层的权限判定入口是
# `enforce_perm(user, perm, db)`; 需要 FastAPI 依赖的调用点请写在 web 层。


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
        from src.db.models import AuditLog

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
    """查询当日剩余试用次数(前端展示用; 日限来自可调免费档)。"""
    used = _trial_used(db, user_id, feature)
    return max(0, effective_trial_limit(db) - used)


# ════════════════════════════════════════════════════════════════════
# 配额检查
# ════════════════════════════════════════════════════════════════════
def check_watchlist_quota(db: Session, user) -> None:
    """普通账号自选≤10。"""
    role = normalize_role(getattr(user, "role", None))
    if role in (ROLE_PRO, ROLE_OWNER):
        return
    from src.db.models import Stock

    limit = MEMBER_WATCHLIST_MAX
    try:
        from src.core import free_tier

        limit = free_tier.member_watchlist_max(db)
    except Exception:  # noqa: BLE001
        pass
    n = db.query(Stock).filter(Stock.user_id == user.id).count()
    if n >= limit:
        raise HTTPException(403, f"普通账号自选上限 {limit} 只, 升级 Pro 可无限")


def check_alert_quota(db: Session, user) -> None:
    """普通账号预警≤3。"""
    role = normalize_role(getattr(user, "role", None))
    if role in (ROLE_PRO, ROLE_OWNER):
        return
    try:
        from src.db.models import PriceAlertRule

        limit = MEMBER_ALERT_MAX
        try:
            from src.core import free_tier

            limit = free_tier.member_alert_max(db)
        except Exception:  # noqa: BLE001
            pass
        n = db.query(PriceAlertRule).filter(PriceAlertRule.user_id == user.id).count()
        if n >= limit:
            raise HTTPException(403, f"普通账号预警上限 {limit} 条, 升级 Pro 可更多")
    except ImportError:
        pass


# ════════════════════════════════════════════════════════════════════
# 设备限制(同账号同时在线 ≤2)
# ════════════════════════════════════════════════════════════════════
def enforce_device_limit(db: Session, user, session_id: str) -> None:
    """登录时: 超过上限(默认 2 台, 运行时可调)踢最早。

    B3(2026-09-29): 上限从 `free_tier.max_sessions_per_user(db)` 读(30s 缓存热生效),
    未配置时回退 `MAX_SESSIONS_PER_USER`(=2) —— 默认行为与收编前一致。
    """
    try:
        from src.db.models import UserSession

        rows = (
            db.query(UserSession)
            .filter(UserSession.user_id == user.id, UserSession.expires_at > datetime.now(timezone.utc))
            .order_by(UserSession.last_seen.asc())
            .all()
        )
        limit = effective_max_sessions(db)
        if len(rows) >= limit:
            # 上限被调小后可能一次超限多个: 从左(最早)起踢到「未过期会话 == 上限」为止
            to_kick = [r for r in rows if r.session_id != session_id]
            kicked: list = []
            while len(rows) - len(kicked) > limit and len(kicked) < len(to_kick):
                oldest = to_kick[len(kicked)]
                # 2026-10-23: 删行必须同时写墓碑, 否则被踢设备的旧 JWT 仍能用
                # (鉴权侧对「无会话行」是放行的, 见 session_is_active 第 4 点)。
                mark_session_revoked(db, oldest.session_id, oldest.expires_at)
                db.delete(oldest)
                kicked.append(oldest)
            if kicked:
                db.commit()
                logger.info(
                    "设备限制(上限 %d): 踢掉 %s 的会话 %s",
                    limit, user.username, kicked[0].session_id[:8],
                )
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.debug("设备限制检查失败: %r", e)


def record_session(
    db: Session,
    user,
    session_id: str,
    expires_at: datetime,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """登录成功后记录会话(设备限制用)。

    P0(2026-10-23): `session_id` 必须是 JWT 的 `jti`(每次签发唯一)。
    历史实现传 `token[:32]` —— JWT 头部恒定, 全平台同值, 会话表被压成一行,
    设备上限形同虚设。
    """
    try:
        from src.db.models import UserSession

        ua = (user_agent or "")[:256] or None
        existing = db.query(UserSession).filter(UserSession.session_id == session_id).first()
        if existing:
            existing.last_seen = datetime.now(timezone.utc)
            existing.expires_at = expires_at
            existing.ip = (ip or existing.ip) or None
            existing.user_agent = ua or existing.user_agent
        else:
            db.add(UserSession(
                session_id=session_id,
                user_id=user.id,
                expires_at=expires_at,
                last_seen=datetime.now(timezone.utc),
                ip=(ip or None),
                user_agent=ua,
            ))
        db.commit()
        enforce_device_limit(db, user, session_id)
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.debug("记录会话失败: %r", e)


def session_is_active(db: Session, session_id: str | None, user_id: str | None = None) -> bool:
    """会话是否仍存活(未被踢、未过期)。**fail-open** 为本函数的硬约束。

    这是「踢设备」真正生效的关键: 只写会话表而鉴权不查表, 旧 JWT 仍能在
    剩余有效期(默认 12h)内调所有接口。

    判定顺序:
      1. 无 session_id(老 token 无 jti / 非 JWT 通道) → 放行。
      2. 命中吊销墓碑(`auth_session_revocations`) → 拒绝 —— 这是「踢」的
         唯一确定信号(行已被删且登记了墓碑)。
      3. 会话行存在且已过期 / 属于他人 → 拒绝。
      4. 会话行不存在 → **放行**。原因: `create_token` 还有不经会话表的调用方
         (`src/core/wechat_bot_worker.py` 的机器人 token), 若「无行即拒绝」会
         把这类合法通道打成 401; 且历史 token(jti 从未落库)也会被全站踢出。
      5. 表缺失 / 查询抛任何异常 → 放行。绝不因会话表故障导致全站 401。
    """
    if not session_id:
        return True
    try:
        from src.db.models import UserSession

        if is_session_revoked(db, session_id):
            return False

        row = db.query(UserSession).filter(UserSession.session_id == session_id).first()
        if row is None:
            return True  # 未登记 ≠ 被踢(见 docstring 第 4 点)
        if user_id and row.user_id != user_id:
            return False
        exp = row.expires_at
        if exp is not None:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp <= datetime.now(timezone.utc):
                return False
        return True
    except ImportError:
        return True
    except Exception as e:  # noqa: BLE001 - fail-open 是硬约束
        logger.warning("[auth] 会话存活校验异常, 按放行处理(fail-open): %r", e)
        return True


# ── 会话吊销墓碑(2026-10-23) ─────────────────────────────────────────
# 语义: 「踢设备」= 删会话行 + 写墓碑。只删行是不够的 —— 无行的 token 必须
# 仍能放行(机器人 token / 历史 token 从未登记), 否则鉴权会误杀合法通道;
# 墓碑把「被主动踢掉」与「从未登记」区分开, 让旧 JWT 立刻 401。
# 不用 ORM model: `src/db/models.py` 不在本次改动范围, 这里用幂等 DDL 兜底。
_REVOCATION_DDL = """
CREATE TABLE IF NOT EXISTS auth_session_revocations (
    session_id VARCHAR(64) PRIMARY KEY,
    expires_at TIMESTAMP NOT NULL,
    revoked_at TIMESTAMP NOT NULL
)
"""


def is_session_revoked(db: Session, session_id: str | None) -> bool:
    """该 session 是否已被主动踢下线。墓碑表不可用/查询异常 → False(fail-open)。"""
    if not session_id:
        return False
    try:
        from sqlalchemy import text

        row = db.execute(
            text("SELECT 1 FROM auth_session_revocations WHERE session_id = :sid"),
            {"sid": session_id},
        ).first()
        return row is not None
    except Exception as e:  # noqa: BLE001 - 墓碑不可用不得阻断鉴权
        logger.debug("[auth] 读取会话墓碑失败(按未吊销处理): %r", e)
        return False


def mark_session_revoked(db: Session, session_id: str, expires_at: datetime) -> None:
    """登记墓碑(幂等; 不 commit, 由调用方统一提交)。失败仅告警, 不阻断。"""
    if not session_id:
        return
    try:
        from sqlalchemy import text

        db.execute(text(_REVOCATION_DDL))
        exp = expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        # 顺手清理已自然过期的墓碑(token 过期后无需再记)
        db.execute(
            text("DELETE FROM auth_session_revocations WHERE expires_at < :now"),
            {"now": now},
        )
        db.execute(
            text("DELETE FROM auth_session_revocations WHERE session_id = :sid"),
            {"sid": session_id},
        )
        db.execute(
            text(
                "INSERT INTO auth_session_revocations (session_id, expires_at, revoked_at) "
                "VALUES (:sid, :exp, :now)"
            ),
            {"sid": session_id, "exp": exp, "now": now},
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[auth] 登记会话墓碑失败(该设备踢出可能延迟生效): %r", e)


def revoke_session(db: Session, session_id: str) -> bool:
    """踢掉单个会话: 写墓碑 + 删会话行。返回是否确实删到了行。"""
    try:
        from src.db.models import UserSession

        row = db.query(UserSession).filter(UserSession.session_id == session_id).first()
        if row is None:
            return False
        mark_session_revoked(db, session_id, row.expires_at)
        db.delete(row)
        db.commit()
        return True
    except ImportError:
        return False
    except Exception as e:  # noqa: BLE001
        logger.warning("[auth] 踢出会话失败: %r", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False


def revoke_other_sessions(db: Session, user_id: str, keep_session_id: str | None) -> int:
    """踢掉该用户除当前会话外的所有会话, 返回踢掉数量。"""
    try:
        from src.db.models import UserSession

        rows = db.query(UserSession).filter(UserSession.user_id == user_id).all()
        n = 0
        for row in rows:
            if keep_session_id and row.session_id == keep_session_id:
                continue
            mark_session_revoked(db, row.session_id, row.expires_at)
            db.delete(row)
            n += 1
        if n:
            db.commit()
        return n
    except ImportError:
        return 0
    except Exception as e:  # noqa: BLE001
        logger.warning("[auth] 批量踢出会话失败: %r", e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


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
    "view_auction": ("集合竞价池", "浏览"),
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
