"""用户管理 API — SIDA 权限体系管理端(2026-08-15)。

模型授权: owner 给每个用户分配可用模型, 存 users.permissions JSON 列的 model_access 字段:
    {"mode": "inherit" | "granted" | "deny_all", "model_ids": [int]}
- inherit: 继承全部平台模型(未设置 = inherit)
- granted: 仅使用 model_ids 勾选的模型(空数组 = 显式全禁)
- deny_all: 全部禁用

读写容错: permissions 可能是 None / list(旧预留格式) / dict / 非法 JSON 字符串,
统一归一为 dict; 写回时保留原有其他键。

基础用户 CRUD 与 auth.router(/api/auth/users)并存: 本 router 挂在 /api 前缀下
由 app.py include(prefix="/api"), 提供 /api/users 路径, 功能一致, 互不冲突。
"""
import json
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.core.db_safe import safe_rollback
from src.web.database import get_db
from src.web.api.audit import log_audit
from src.web.api.auth import (
    create_user,
    get_current_user,
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    normalize_email,
    require_owner,
)
from src.web.models import AIModel, AIService, AppSettings, User

logger = logging.getLogger(__name__)
router = APIRouter()

MODEL_ACCESS_MODES = ("inherit", "granted", "deny_all")


# ── permissions 读写容错 ───────────────────────────────────────────────

def _load_permissions(user: User) -> dict:
    """读取用户 permissions(JSON 列), 容错归一为 dict。

    SQLAlchemy JSON 列在库中可能为 None / list(旧预留格式) / dict;
    若该列以字符串形态存在(手工迁移/外部写入), 也尝试 json.loads 解析。
    """
    raw = getattr(user, "permissions", None)
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    return {}


def _get_model_access(user: User) -> dict:
    """读取用户的 model_access, 结构恒定为 {"mode": str, "model_ids": [int]}。"""
    perms = _load_permissions(user)
    ma = perms.get("model_access")
    if not isinstance(ma, dict):
        return {"mode": "inherit", "model_ids": []}
    mode = ma.get("mode", "inherit")
    if mode not in MODEL_ACCESS_MODES:
        mode = "inherit"
    ids = ma.get("model_ids")
    if not isinstance(ids, list):
        ids = []
    clean = []
    for i in ids:
        if isinstance(i, bool) or not isinstance(i, (int, float)):
            continue
        clean.append(int(i))
    return {"mode": mode, "model_ids": sorted(set(clean))}


def _set_model_access(user: User, mode: str, model_ids: list[int]) -> None:
    """写入 model_access 到 permissions, 保留其他原有键。"""
    perms = _load_permissions(user)
    perms["model_access"] = {"mode": mode, "model_ids": list(model_ids)}
    user.permissions = perms


def _list_all_models(db: Session) -> list[dict]:
    """全局模型列表: AIModel join AIService。"""
    rows = (
        db.query(AIModel)
        .join(AIService)
        .order_by(AIModel.id.asc())
        .all()
    )
    return [
        {
            "id": m.id,
            "name": m.name,
            "model": m.model,
            "service_name": m.service.name if m.service else None,
        }
        for m in rows
    ]


# ── 基础用户 CRUD(与 auth.router 并存) ────────────────────────────────

class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "member"


class UserUpdateRequest(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


# ── 模型授权(权限体系管理端) ──────────────────────────────────────────
# 注: 用户 CRUD 在 auth.py(/api/auth/users), 本文件只负责 model-access

class ModelAccessUpdate(BaseModel):
    mode: str
    # mode=granted 时必填(可为空数组=显式全禁); inherit/deny_all 时忽略
    model_ids: Optional[list[int]] = None


@router.get("/{uid}/model-access")
def get_user_model_access(
    uid: str,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """获取某用户的模型授权(仅 owner)。未设置 = inherit(继承全部平台模型)。"""
    target = get_user_by_id(db, uid)
    if not target:
        raise HTTPException(404, "用户不存在")
    access = _get_model_access(target)
    return {
        "mode": access["mode"],
        "model_ids": access["model_ids"],
        "all_models": _list_all_models(db),
        "user_role": target.role,
        "username": target.username,
    }


@router.put("/{uid}/model-access")
def update_user_model_access(
    uid: str,
    data: ModelAccessUpdate,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """写入用户的模型授权(仅 owner)。保留 permissions 其他键。"""
    target = get_user_by_id(db, uid)
    if not target:
        raise HTTPException(404, "用户不存在")
    if data.mode not in MODEL_ACCESS_MODES:
        raise HTTPException(400, f"mode 必须是 {', '.join(MODEL_ACCESS_MODES)} 之一")
    if data.mode == "granted":
        if data.model_ids is None:
            raise HTTPException(400, "mode=granted 时必须提供 model_ids(空数组=显式全禁)")
        model_ids = sorted(set(data.model_ids))
        if model_ids:
            rows = db.query(AIModel.id).filter(AIModel.id.in_(model_ids)).all()
            existing = {r[0] for r in rows}
            missing = [i for i in model_ids if i not in existing]
            if missing:
                raise HTTPException(400, f"模型不存在: {missing}")
    else:
        # inherit / deny_all 忽略 model_ids, 统一写空数组保证结构恒定
        model_ids = []

    _set_model_access(target, data.mode, model_ids)
    db.commit()
    _audit_admin(
        db, owner, "model_access",
        f"用户 {target.username}({target.id}) model_access={data.mode} ids={model_ids}",
    )
    logger.info(f"更新用户 {target.username}({target.id}) 模型授权: {data.mode} {model_ids}")
    return {"mode": data.mode, "model_ids": model_ids}


# ── 模块权限(功能模块授权) ─────────────────────────────────────────────

class PermissionUpdate(BaseModel):
    # 白名单权限点列表(勾选 = 在角色基础权限上追加授权)
    permissions: list[str] = []


def _read_permission_list(perms) -> list[str]:
    """从 users.permissions 兼容两种形态读取权限点白名单。"""
    if isinstance(perms, dict):
        return [p for p in perms.get("permissions", []) if isinstance(p, str)]
    if isinstance(perms, list):
        return [p for p in perms if isinstance(p, str)]
    return []


@router.get("/me/permissions")
def get_my_permissions(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """当前用户自己的模块权限(前端导航过滤用)。

    返回 role_defaults(角色自带)+ granted(额外授权)的并集,
    前端据此隐藏未授权模块的导航入口。
    member 的试用功能(view_opportunities 等)也进 effective, 前端展示入口,
    后端 enforce_perm 仍按 3 次/天限流。
    """
    from src.core.permissions import (
        PERMISSION_LABELS,
        effective_trial_features,
        effective_trial_limit,
        get_role_permissions,
        get_trial_remaining,
        normalize_role,
    )

    # 2026-09-18: 试用功能/日限改为**运行时可调**(free_tier 配置), 不再读模块常量
    trial_features = effective_trial_features(db)
    trial_limit = effective_trial_limit(db)

    role_defaults = sorted(get_role_permissions(user.role))
    granted = _read_permission_list(user.permissions)
    # member 试用功能: 进 effective 让前端显示入口, trial_remaining 供 UI 展示
    trial_info: dict[str, int] = {}
    role = normalize_role(user.role)
    if role == "member":
        for feat in trial_features:
            trial_info[feat] = get_trial_remaining(db, user.id, feat)
    effective = set(role_defaults) | set(granted) | set(trial_info.keys())
    all_permissions = [
        {"key": k, "label": v[0], "group": v[1]} for k, v in PERMISSION_LABELS.items()
    ]
    return {
        "username": user.username,
        "role": user.role,
        "granted": granted,
        "role_defaults": role_defaults,
        "effective": sorted(effective),
        "trial": trial_info,
        "trial_daily_limit": trial_limit,
        "all_permissions": all_permissions,
    }


@router.get("/{uid}/permissions")
def get_user_permissions(
    uid: str,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """获取某用户的模块权限(仅 owner)。

    granted: 白名单追加授权(可编辑); role_defaults: 角色自带权限(只读展示,
    让 owner 一眼看到该用户已有哪些权限)。
    """
    del owner
    target = get_user_by_id(db, uid)
    if not target:
        raise HTTPException(404, "用户不存在")
    from src.core.permissions import PERMISSION_LABELS, get_role_permissions

    all_permissions = [
        {"key": k, "label": v[0], "group": v[1]} for k, v in PERMISSION_LABELS.items()
    ]
    return {
        "username": target.username,
        "role": target.role,
        "granted": _read_permission_list(target.permissions),
        "role_defaults": sorted(get_role_permissions(target.role)),
        "all_permissions": all_permissions,
    }


@router.put("/{uid}/permissions")
def update_user_permissions(
    uid: str,
    body: PermissionUpdate,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """写入用户的模块权限白名单(仅 owner)。保留 model_access 等其他键。"""
    target = get_user_by_id(db, uid)
    if not target:
        raise HTTPException(404, "用户不存在")
    from src.core.permissions import PERMISSION_LABELS

    valid = set(PERMISSION_LABELS.keys())
    invalid = set(body.permissions) - valid
    if invalid:
        raise HTTPException(400, f"无效权限点: {sorted(invalid)}")

    # 保留 permissions dict 里的 model_access 等其他键
    if isinstance(target.permissions, dict):
        merged = dict(target.permissions)
    elif isinstance(target.permissions, list):
        merged = {"permissions": target.permissions}
    else:
        merged = {}
    merged["permissions"] = list(dict.fromkeys(body.permissions))  # 去重保序
    target.permissions = merged
    db.commit()
    _audit_admin(
        db, owner, "permissions",
        f"用户 {target.username}({target.id}) permissions={merged['permissions']}",
    )
    logger.info(f"更新用户 {target.username}({target.id}) 模块权限: {merged['permissions']}")
    return {"permissions": merged["permissions"]}


# ── Admin 管理后台(2026-09-16): 用户列表/启停/改角色/注册统计 ────────

ROLE_CHOICES = ("guest", "member", "pro", "owner")


def _audit_admin(db: Session, actor: User, action: str, detail: str) -> None:
    """管理端写操作统一审计(审计页可见)。失败不阻塞业务。"""
    try:
        log_audit(db, actor, "manage_user", detail=f"{action}: {detail}"[:255])
    except Exception as e:  # noqa: BLE001
        logger.warning("admin 审计写入失败 action=%s: %r", action, e)


def _admin_user_row(db: Session, u: User) -> dict:
    """用户列表行: 基础字段 + last_login(audit login 最近一条) + 注销态。"""
    last_login = None
    try:
        from src.web.models import AuditLog

        row = (
            db.query(AuditLog.created_at)
            .filter(AuditLog.user_id == str(u.id), AuditLog.action == "login")
            .order_by(AuditLog.created_at.desc())
            .first()
        )
        if row and row[0]:
            last_login = row[0].isoformat()
    except Exception:  # noqa: BLE001
        # 不回滚会把审计表查询失败传染给整个列表接口(生产表现为「后台数据没了」)
        safe_rollback(db)
        last_login = None
    return {
        "id": str(u.id),
        "username": u.username or "",
        "email": getattr(u, "email", None),
        "role": u.role or "member",
        "is_active": bool(u.is_active),
        "is_deleted": bool(getattr(u, "is_deleted", False)),
        "deleted_at": (
            u.deleted_at.isoformat() if getattr(u, "deleted_at", None) else None
        ),
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "last_login": last_login,
    }


@router.get("/admin/list")
def admin_list_users(
    _owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """列出全部用户(仅 owner)。按创建时间倒序。

    含 30 天宽限期内的软删除账号(is_deleted=True), 由前端打「已注销」徽标并提供恢复。
    """
    rows = db.query(User).order_by(User.created_at.desc()).all()
    return {"users": [_admin_user_row(db, u) for u in rows]}


class AdminToggleActiveRequest(BaseModel):
    is_active: Optional[bool] = None  # None = 取反


class AdminChangeRoleRequest(BaseModel):
    role: str


class AdminCreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "member"
    email: Optional[str] = None


class AdminResetPasswordRequest(BaseModel):
    new_password: str


@router.post("/admin/create")
def admin_create_user(
    body: AdminCreateUserRequest,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """新建用户(仅 owner): 用户名/密码/角色/邮箱。

    与「设置 → 用户管理」的建号能力对齐(此前后台只能看不能建)。
    """
    username = (body.username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9]{2,20}", username):
        raise HTTPException(400, "用户名需为 2-20 位字母或数字")
    if get_user_by_username(db, username):
        raise HTTPException(400, "用户名已存在")
    if len(body.password or "") < 8:
        raise HTTPException(400, "密码长度至少 8 位")
    role = (body.role or "member").strip().lower()
    if role not in ROLE_CHOICES:
        raise HTTPException(400, f"role 必须是 {', '.join(ROLE_CHOICES)} 之一")
    email = normalize_email(body.email) if body.email else None
    if email:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise HTTPException(400, "邮箱格式不正确")
        if get_user_by_email(db, email):
            raise HTTPException(400, "该邮箱已注册")

    target = create_user(db, username, body.password, role=role, email=email)
    _audit_admin(db, owner, "create", f"新建用户 {username}({target.id}) role={role}")
    logger.info("admin create user=%s role=%s by=%s", username, role, owner.username)
    return _admin_user_row(db, target)


@router.post("/admin/{uid}/reset-password")
def admin_reset_password(
    uid: str,
    body: AdminResetPasswordRequest,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """重置某用户密码(仅 owner)。写审计并踢掉该用户旧 token。"""
    target = get_user_by_id(db, uid)
    if not target:
        raise HTTPException(404, "用户不存在")
    pwd = body.new_password or ""
    if len(pwd) < 8:
        raise HTTPException(400, "密码长度至少 8 位")
    from src.web.api.auth import hash_password

    target.password_hash = hash_password(pwd)
    target.token_version = (target.token_version or 1) + 1
    db.commit()
    _audit_admin(db, owner, "reset_password", f"重置用户 {target.username}({target.id}) 密码")
    logger.info("admin reset-password user=%s by=%s", target.username, owner.username)
    return {"id": str(target.id), "username": target.username, "reset": True}


@router.post("/admin/{uid}/restore")
def admin_restore_user(
    uid: str,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """恢复 30 天宽限期内的注销账号(仅 owner)。

    注销是软删(users.is_deleted + deleted_at), 30 天后由 purge 物理清除;
    宽限期内 owner 可在这里撤销: 清标记 + 激活 + token_version+1。
    """
    target = get_user_by_id(db, uid)
    if not target:
        raise HTTPException(404, "用户不存在")
    if not getattr(target, "is_deleted", False) and not getattr(target, "deleted_at", None):
        raise HTTPException(400, "该账号未处于注销状态, 无需恢复")
    target.is_deleted = False
    target.deleted_at = None
    target.is_active = True
    target.token_version = (target.token_version or 1) + 1
    db.commit()
    _audit_admin(db, owner, "restore", f"恢复注销账号 {target.username}({target.id})")
    logger.info("admin restore user=%s by=%s", target.username, owner.username)
    return _admin_user_row(db, target)


@router.post("/admin/{uid}/toggle-active")
def admin_toggle_active(
    uid: str,
    body: AdminToggleActiveRequest,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """启用/禁用用户(仅 owner)。不能禁用自己。"""
    target = get_user_by_id(db, uid)
    if not target:
        raise HTTPException(404, "用户不存在")
    if str(target.id) == str(owner.id) and body.is_active is False:
        raise HTTPException(400, "不能禁用自己")
    if body.is_active is None:
        target.is_active = not bool(target.is_active)
    else:
        target.is_active = bool(body.is_active)
    if not target.is_active:
        target.token_version = (target.token_version or 1) + 1  # 踢掉旧 token
    db.commit()
    _audit_admin(
        db, owner, "toggle_active", f"用户 {target.username}({target.id}) active={target.is_active}"
    )
    logger.info(f"admin toggle-active user={target.username}({target.id}) active={target.is_active}")
    return {"id": str(target.id), "is_active": bool(target.is_active)}


@router.post("/admin/{uid}/change-role")
def admin_change_role(
    uid: str,
    body: AdminChangeRoleRequest,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """修改用户角色 guest/member/pro/owner(仅 owner)。不能取消自己的 owner。

    改角色会同步 API Key 档位(此前只改 users.role, 手工设 pro 后 Key 仍是 free):
    - 升级为 pro → 该用户 active key 全部升 pro 档(与审核通过同一函数)
    - 从 pro 降为其它 → active key 降回 free 并写 TierDowngradeLog
    """
    target = get_user_by_id(db, uid)
    if not target:
        raise HTTPException(404, "用户不存在")
    role = (body.role or "").strip().lower()
    if role not in ROLE_CHOICES:
        raise HTTPException(400, f"role 必须是 {', '.join(ROLE_CHOICES)} 之一")
    if str(target.id) == str(owner.id) and role != "owner":
        raise HTTPException(400, "不能取消自己的 owner 角色")

    prev_role = (target.role or "member").strip().lower()
    target.role = role
    # 权限随角色变: 让旧 token 失效, 强制重新登录取新角色
    if role != prev_role:
        target.token_version = (target.token_version or 1) + 1
    db.commit()

    keys_changed = 0
    try:
        from src.web.api.pro_billing import (
            _upgrade_user_keys_to_pro,
            downgrade_user_keys_to_free,
        )

        if role == "pro" and prev_role != "pro":
            keys_changed = _upgrade_user_keys_to_pro(db, str(target.id))
        elif prev_role == "pro" and role != "pro":
            keys_changed = downgrade_user_keys_to_free(
                db, str(target.id), reason=f"admin 改角色 {prev_role}→{role}"
            )
    except Exception as e:  # noqa: BLE001 — 档位同步失败不回滚角色(role 已落库), 但要留痕
        logger.error("admin change-role 同步 key 档位失败 user=%s: %r", target.username, e)

    _audit_admin(
        db,
        owner,
        "change_role",
        f"用户 {target.username}({target.id}) {prev_role}→{role} keys={keys_changed}",
    )
    logger.info(f"admin change-role user={target.username}({target.id}) role={role}")
    return {"id": str(target.id), "role": target.role, "keys_changed": keys_changed}


# ── 注册模式(2026-10-23): 后台三态切换 ───────────────────────────────

class RegisterModeRequest(BaseModel):
    mode: str = ""  # invite / open / closed


def set_register_mode(db: Session, actor: User, mode: str) -> dict:
    """写入 app_settings.register_mode 并立即生效(owner only 由调用方保证)。

    `core.invite_codes.get_register_mode` 每次现读 DB(无缓存), 所以本函数 return 时
    注册流程已经按新模式判定 —— 不需要重启或清缓存。
    """
    from src.core.invite_codes import VALID_MODES, get_register_mode

    prev = get_register_mode(db)
    m = (mode or "").strip().lower()
    if m not in VALID_MODES:
        raise HTTPException(400, f"mode 必须是 {'/'.join(VALID_MODES)} 之一")
    row = db.query(AppSettings).filter(AppSettings.key == "register_mode").first()
    if row:
        row.value = m
    else:
        db.add(AppSettings(
            key="register_mode",
            value=m,
            description="注册模式: invite(邀请制)/open(开放注册)/closed(关闭注册)",
        ))
    db.commit()
    current = get_register_mode(db)
    _audit_admin(db, actor, "register_mode", f"注册模式 {prev}→{current}")
    logger.info("admin register-mode %s→%s by=%s", prev, current, actor.username)
    return {"mode": current, "previous": prev}


@router.put("/admin/register-mode")
def admin_put_register_mode(
    body: RegisterModeRequest,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """切换注册模式(仅 owner)。等价路径: `/api/admin/register-mode`(pro_billing 转发)。"""
    return set_register_mode(db, owner, body.mode)


@router.get("/admin/stats")
def admin_registration_stats(
    _owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """注册统计(仅 owner): 总量/活跃/新增/按角色/最近注册。

    时区: users.created_at 存 naive UTC(server_default=func.now()), 故比较基准必须
    是 UTC —— 原实现用 `datetime.now()`(本地 CST)导致每天 00:00–08:00 的统计偏 8h。
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    d7 = now - timedelta(days=7)
    d30 = now - timedelta(days=30)

    total_users = db.query(func.count(User.id)).scalar() or 0
    active_users = db.query(func.count(User.id)).filter(User.is_active.is_(True)).scalar() or 0
    new_today = db.query(func.count(User.id)).filter(User.created_at >= day_start).scalar() or 0
    new_7d = db.query(func.count(User.id)).filter(User.created_at >= d7).scalar() or 0
    new_30d = db.query(func.count(User.id)).filter(User.created_at >= d30).scalar() or 0

    by_role: dict[str, int] = {"guest": 0, "member": 0, "pro": 0, "owner": 0}
    for role, n in db.query(User.role, func.count(User.id)).group_by(User.role).all():
        key = (role or "member").strip().lower()
        if key == "admin":
            key = "owner"
        if key not in by_role:
            by_role[key] = 0
        by_role[key] = by_role.get(key, 0) + int(n or 0)

    recent = (
        db.query(User)
        .order_by(User.created_at.desc(), User.id.desc())
        .limit(10)
        .all()
    )
    return {
        "total_users": int(total_users),
        "active_users": int(active_users),
        "new_today": int(new_today),
        "new_7d": int(new_7d),
        "new_30d": int(new_30d),
        "by_role": by_role,
        "recent_registrations": [
            {
                "username": u.username or "",
                "email": getattr(u, "email", None),
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in recent
        ],
    }


@router.get("/admin/usage-report")
def admin_usage_report(
    days: int = 7,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    """后台用量报表(2026-09-15 C.2): 按用户/按接口/按天聚合高价值接口调用。"""
    del owner
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func

    from src.db.models import HighValueApiLog

    since = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 90)))

    # 按用户+接口聚合
    rows = (
        db.query(
            HighValueApiLog.user_id,
            HighValueApiLog.username,
            HighValueApiLog.api_name,
            func.count(HighValueApiLog.id).label("calls"),
            func.max(HighValueApiLog.created_at).label("last_at"),
        )
        .filter(HighValueApiLog.created_at >= since)
        .group_by(HighValueApiLog.user_id, HighValueApiLog.username, HighValueApiLog.api_name)
        .order_by(func.count(HighValueApiLog.id).desc())
        .all()
    )

    # 按天聚合
    daily_rows = (
        db.query(
            func.date(HighValueApiLog.created_at).label("day"),
            HighValueApiLog.api_name,
            func.count(HighValueApiLog.id).label("calls"),
        )
        .filter(HighValueApiLog.created_at >= since)
        .group_by(func.date(HighValueApiLog.created_at), HighValueApiLog.api_name)
        .order_by(func.date(HighValueApiLog.created_at).desc())
        .all()
    )

    return {
        "since": since.isoformat(),
        "days": days,
        "by_user_api": [
            {
                "user_id": r.user_id,
                "username": r.username,
                "api_name": r.api_name,
                "calls": r.calls,
                "last_at": r.last_at.isoformat() if r.last_at else None,
            }
            for r in rows
        ],
        "by_day": [
            {"day": str(r.day), "api_name": r.api_name, "calls": r.calls}
            for r in daily_rows
        ],
    }
