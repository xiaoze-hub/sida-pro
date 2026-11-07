"""Transaction-aware primitives for explicitly confirmed settings proposals.

Callers own the transaction and must roll back on all errors. This module is not
an agent tool or an HTTP route; it cannot itself create or confirm a proposal.

2026-11-07 安全审计修复（原 ab32ce67 原型的问题）:
  ① 空字符串是「继承 env / 关闭项」语义，不是非法值。原 validate_setting 对
     空值一律拒绝，与「关闭静默期」这类合法意图冲突。这里明确区分:
       - absent（无该 key 的行）→ before_present=False
       - NULL（有行但 value 为 NULL）→ before_present=True, before=None
       - 空字符串（有行且 value=''）→ before_present=True, before=''
     三者互不混淆，CAS 只按真实行状态判定，绝不把 NULL/空/absent 当成一回事。
  ② CAS 更新改用 SELECT ... FOR UPDATE（populate_existing + with_for_update）
     而非 ORM 的 bulk `.update()`，保证已加载对象与 DB 行一致、读到的 before
     就是锁定的那一行，不会在并发下读到过期快照再误判冲突。
  ③ 不存在的行并发 insert 冲突用 SAVEPOINT 局部回滚，把 IntegrityError 翻译成
     明确的 SettingConflict(409)，不回滚调用方整笔事务。
  ④ 不再接受模型自造的 proposal dict。apply 绑定到一条已持久化的 CopilotAction
     行：actor/kind/pending 来自该行，key/before/value 从该行 payload 读取，
     audit 的 action_id 必须是同一行；绝不允许伪造或跨用户。
  ⑤ propose/apply 权限各自依据实际 DB 用户角色 + 权限扩展实时判定；失权/禁用/
     服务身份(service principal)一律拒绝，用不同错误码区分(401 无权限 vs 404 不存在)。
  ⑥ 敏感值/任意 key/注入一律拒绝：key 只收白名单三键，值只收 validate 放行的形态。
  ⑦ 设置类动作不支持撤销回滚：只支持「重新提议修正」。不暴露可撤销标记。
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.core.permissions import get_role_permissions
from src.db.models import AppSettings, AuditLog, User
from src.harness.setting_policy import validate_setting


class SettingPermissionError(ValueError):
    """Current user cannot modify global settings (401)."""

    status_code = 401


class SettingNotFound(ValueError):
    """Referenced action row absent or not owned by this user (404)."""

    status_code = 404


class SettingConflict(ValueError):
    """The setting changed since the proposal was prepared (409)."""

    status_code = 409


def _authorized_user(db: Session, user_id: Any) -> User:
    """Load + lock the user row and verify live role/permissions (401 on deny)."""
    user = (
        db.query(User)
        .filter(User.id == str(user_id))
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    # 服务身份 / 禁用 / 软删 / 演示 / guest 一律拒绝。
    if (
        user is None
        or getattr(user, "is_service", False)
        or not bool(getattr(user, "is_active", True))
        or bool(getattr(user, "is_deleted", False))
        or getattr(user, "username", "") == "demo"
        or getattr(user, "role", "") in ("guest", "service")
    ):
        raise SettingPermissionError("无权修改平台设置")
    extra = getattr(user, "permissions", None) or []
    if isinstance(extra, dict):
        extra = extra.get("permissions", [])
    if not isinstance(extra, list):
        extra = []
    perms = get_role_permissions(user.role) | {p for p in extra if isinstance(p, str)}
    if "manage_settings" not in perms:
        raise SettingPermissionError("无权修改平台设置")
    return user


def prepare_setting(db: Session, user_id: Any, key: str, value: str) -> dict:
    """Read proposal baseline; this function does not write application settings.

    Returns a dict shaped as the *persisted payload* to attach to a CopilotAction
    row (actor/kind/pending are set by actions.propose, not here).
    """
    _authorized_user(db, user_id)
    val = validate_setting(key, value)
    row = (
        db.query(AppSettings)
        .filter(AppSettings.key == key)
        .populate_existing()
        .one_or_none()
    )
    # 区分 absent / NULL / 空字符串: 界面如实显示, CAS 按真实行状态判定。
    return {
        "key": key,
        "value": val,
        "before_present": row is not None,
        "before": row.value if row is not None else None,
    }


def _lock_setting_row(db: Session, key: str):
    """SELECT ... FOR UPDATE on the settings row; returns the row or None."""
    return (
        db.query(AppSettings)
        .filter(AppSettings.key == key)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )


def apply_setting(db: Session, user_id: Any, payload: dict, *, action_id: int) -> None:
    """CAS-write + audit in the caller's transaction (never commits).

    `payload` comes from a *persisted CopilotAction row* (key/before/before_present/
    value), not from a model-supplied dict. We re-derive `before`/`before_present`
    from the live DB row under lock and require they match the proposal; anything
    else is a 409 conflict. Sensitive/out-of-scope keys and values are rejected by
    validate_setting before any write.
    """
    user = _authorized_user(db, user_id)
    if not isinstance(payload, dict):
        raise SettingConflict("提议缺少基线，请重新提议")
    key = payload.get("key")
    val = validate_setting(key, payload.get("value"))
    # 只接受提议里声明的基线形态；拒绝模型塞入的未知字段组合。
    claimed_present = payload.get("before_present")
    if not isinstance(claimed_present, bool) or "before" not in payload:
        raise SettingConflict("提议缺少基线，请重新提议")

    row = _lock_setting_row(db, key)
    present = row is not None
    before = row.value if row is not None else None

    if present:
        # 现状是「有行」，要求提议也声明「有行」且 before 完全一致。
        if not claimed_present or before != payload.get("before"):
            raise SettingConflict("该设置已被修改，请重新提议")
        row.value = val
    else:
        # 现状「无行」: 提议必须声明 absent 且 before 为 None。
        if claimed_present or payload.get("before") is not None:
            raise SettingConflict("该设置已被创建，请重新提议")
        db.add(AppSettings(key=key, value=val, description=""))
        # 并发 insert 冲突用 SAVEPOINT 局部回滚，不破坏调用方事务。
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError as exc:
            raise SettingConflict("该设置已被创建，请重新提议") from exc

    db.add(
        AuditLog(
            user_id=str(user_id),
            username=user.username,
            action="copilot_settings_update",
            detail=json.dumps(
                {
                    "action_id": action_id,
                    "key": key,
                    "before": before,
                    "before_present": present,
                    "after": val,
                },
                ensure_ascii=False,
            ),
        )
    )
    db.flush()
