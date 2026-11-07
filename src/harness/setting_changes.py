"""Transaction-aware primitives for explicitly confirmed settings proposals.

Callers own the transaction and must roll back on all errors. This module is not
an agent tool or an HTTP route; it cannot itself create or confirm a proposal.
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
    """Current user cannot modify global settings."""


class SettingConflict(ValueError):
    """The setting changed since the proposal was prepared."""


def _authorized_user(db: Session, user_id: Any) -> User:
    user = (db.query(User).filter(User.id == str(user_id))
            .populate_existing().with_for_update().one_or_none())
    if user is None or not user.is_active or user.is_deleted or user.username == 'demo' or user.role == 'guest':
        raise SettingPermissionError('无权修改平台设置')
    extra = user.permissions or []
    if isinstance(extra, dict):
        extra = extra.get('permissions', [])
    if not isinstance(extra, list):
        extra = []
    perms = get_role_permissions(user.role) | {p for p in extra if isinstance(p, str)}
    if 'manage_settings' not in perms:
        raise SettingPermissionError('无权修改平台设置')
    return user


def prepare_setting(db: Session, user_id: Any, key: str, value: str) -> dict:
    """Read proposal baseline; this function does not write application settings."""
    _authorized_user(db, user_id)
    val = validate_setting(key, value)
    row = db.query(AppSettings).filter(AppSettings.key == key).populate_existing().one_or_none()
    return {'key': key, 'value': val, 'before_present': row is not None,
            'before': row.value if row is not None else None}


def apply_setting(db: Session, user_id: Any, proposal: dict, *, action_id: int) -> None:
    """CAS-write and append audit in the caller's transaction (never commits)."""
    user = _authorized_user(db, user_id)
    if not isinstance(proposal, dict) or not isinstance(proposal.get('before_present'), bool) or 'before' not in proposal:
        raise SettingConflict('提议缺少基线，请重新提议')
    key = proposal.get('key')
    val = validate_setting(key, proposal.get('value'))
    before = proposal['before']
    if proposal['before_present']:
        if before is not None and not isinstance(before, str):
            raise SettingConflict('提议基线无效，请重新提议')
        changed = db.query(AppSettings).filter(AppSettings.key == key, AppSettings.value == before).update(
            {AppSettings.value: val}, synchronize_session=False
        )
        if changed != 1:
            raise SettingConflict('该设置已被修改，请重新提议')
    else:
        if before is not None or db.query(AppSettings.id).filter(AppSettings.key == key).first():
            raise SettingConflict('该设置已被创建，请重新提议')
        db.add(AppSettings(key=key, value=val, description=''))
        try:
            db.flush()
        except IntegrityError as exc:
            raise SettingConflict('该设置已被创建，请重新提议') from exc
    db.add(AuditLog(user_id=str(user_id), username=user.username, action='copilot_settings_update',
                    detail=json.dumps({'action_id': action_id, 'key': key, 'before': before,
                                       'before_present': proposal['before_present'], 'after': val},
                                      ensure_ascii=False)))
    db.flush()
