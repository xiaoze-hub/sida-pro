"""JWT 密钥自动轮换 (P0 安全加固, 2026-09-18)

设计:
- 轮换策略: 每 90 天自动生成新 JWT_SECRET
- 旧 secret 在 grace period(默认 7 天, 可配置 SECRET_ROTATION_GRACE_DAYS)内仍可验证
- 轮换记录落 audit_logs(action=rotate_jwt_secret)
- 手动触发: POST /api/admin/rotate-secrets (owner only, 见 src/web/api/admin_secrets.py)

存储(AppSettings):
- jwt_secret              : 当前签名密钥(与 auth_tokens 共用同一 key)
- jwt_secret_previous     : JSON 数组 [{"secret","rotated_at","expires_at"}, ...]
- jwt_secret_rotated_at   : ISO8601 上次轮换时间(无则视为从未轮换)

约束:
- env JWT_SECRET(>=32B) 被 pin 时无法通过轮换改变签发密钥 → 自动/手动轮换跳过并说明
- 轮换后必须 invalidate_jwt_secret_cache(), 否则本进程继续用旧缓存签发
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# AppSettings keys
JWT_SECRET_KEY = "jwt_secret"
JWT_PREVIOUS_KEY = "jwt_secret_previous"
JWT_ROTATED_AT_KEY = "jwt_secret_rotated_at"

# 轮换策略
ROTATION_INTERVAL_DAYS = int(os.getenv("SECRET_ROTATION_INTERVAL_DAYS", "90"))
# grace period: 旧 secret 可验签窗口。默认 7 天(远大于 12h token TTL, 覆盖跨周末会话)
GRACE_PERIOD_DAYS = int(os.getenv("SECRET_ROTATION_GRACE_DAYS", "7"))

JOB_ID = "jwt_secret_rotation"

# 进程内 grace secrets 缓存: decode_token 每请求调用, 避免每次打 DB。
# 轮换后 rotate_jwt_secret 会清缓存; 另设 60s TTL 兜底多进程场景。
_GRACE_CACHE: list[str] | None = None
_GRACE_CACHE_TS: float = 0.0
_GRACE_CACHE_TTL = float(os.getenv("SECRET_ROTATION_CACHE_TTL", "60"))


def _invalidate_grace_cache() -> None:
    global _GRACE_CACHE, _GRACE_CACHE_TS
    _GRACE_CACHE = None
    _GRACE_CACHE_TS = 0.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _env_secret_pinned() -> bool:
    """env JWT_SECRET 是否处于生效状态(>=32 字节, 与 auth_tokens 口径一致)。"""
    env_secret = os.getenv("JWT_SECRET") or ""
    return len(env_secret.encode("utf-8")) >= 32


def get_previous_records() -> list[dict]:
    """读取旧密钥环(未过期条目), 失败返回空列表。"""
    from src.db.session import SessionLocal
    from src.db.models import AppSettings

    db = SessionLocal()
    try:
        row = db.query(AppSettings).filter(AppSettings.key == JWT_PREVIOUS_KEY).first()
        raw = getattr(row, "value", None) if row else None
        if not raw:
            return []
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("[secret_rotation] jwt_secret_previous 非法 JSON, 已忽略")
            return []
        if not isinstance(items, list):
            return []
        now = _utc_now()
        valid: list[dict] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("secret"):
                continue
            expires_at = _parse_iso(item.get("expires_at"))
            if expires_at and expires_at < now:
                continue
            valid.append(item)
        return valid
    finally:
        db.close()


def get_grace_secrets() -> list[str]:
    """返回 grace period 内仍可验签的旧 secret 列表(不含当前 secret)。

    带 60s 进程内缓存(decode 热路径); 轮换写路径会主动失效。
    """
    global _GRACE_CACHE, _GRACE_CACHE_TS
    import time as _time

    now = _time.monotonic()
    if _GRACE_CACHE is not None and (now - _GRACE_CACHE_TS) < _GRACE_CACHE_TTL:
        return list(_GRACE_CACHE)
    secrets_list = [str(r["secret"]) for r in get_previous_records() if r.get("secret")]
    _GRACE_CACHE = secrets_list
    _GRACE_CACHE_TS = now
    return list(secrets_list)


def _save_previous_records(items: list[dict]) -> None:
    from src.db.session import SessionLocal
    from src.db.models import AppSettings

    db = SessionLocal()
    try:
        row = db.query(AppSettings).filter(AppSettings.key == JWT_PREVIOUS_KEY).first()
        payload = json.dumps(items, ensure_ascii=False)
        if row:
            row.value = payload
            row.description = "JWT历史密钥(轮换grace period, JSON)"
        else:
            db.add(AppSettings(
                key=JWT_PREVIOUS_KEY,
                value=payload,
                description="JWT历史密钥(轮换grace period, JSON)",
            ))
        db.commit()
    finally:
        db.close()


def _set_rotated_at(dt: datetime) -> None:
    from src.db.session import SessionLocal
    from src.db.models import AppSettings

    db = SessionLocal()
    try:
        row = db.query(AppSettings).filter(AppSettings.key == JWT_ROTATED_AT_KEY).first()
        if row:
            row.value = _iso(dt)
        else:
            db.add(AppSettings(
                key=JWT_ROTATED_AT_KEY,
                value=_iso(dt),
                description="JWT密钥上次轮换时间(UTC ISO)",
            ))
        db.commit()
    finally:
        db.close()


def get_last_rotated_at() -> datetime | None:
    from src.db.session import SessionLocal
    from src.db.models import AppSettings

    db = SessionLocal()
    try:
        row = db.query(AppSettings).filter(AppSettings.key == JWT_ROTATED_AT_KEY).first()
        return _parse_iso(getattr(row, "value", None) if row else None)
    finally:
        db.close()


def days_until_next_rotation() -> float | None:
    """距下次应轮换的天数; 从未轮换返回 0.0(应尽快轮换); 失败 None。"""
    last = get_last_rotated_at()
    if last is None:
        return 0.0
    due = last + timedelta(days=ROTATION_INTERVAL_DAYS)
    delta = (due - _utc_now()).total_seconds() / 86400.0
    return max(0.0, delta)


def rotate_jwt_secret(
    actor: str = "system",
    grace_days: int | None = None,
) -> dict:
    """轮换 JWT secret: 生成新密钥 → 旧密钥入 grace 环 → 清缓存 → 审计。

    Returns:
        {"rotated": bool, "reason": str, "grace_days": int, "previous_count": int}

    Raises:
        RuntimeError: 仅在 DB 写失败等硬错误时抛出(调用方需处理)。
    """
    from src.core.auth_tokens import invalidate_jwt_secret_cache

    if _env_secret_pinned():
        reason = "env JWT_SECRET 已 pin(>=32B), 轮换无法改变签发密钥, 已跳过"
        logger.warning("[secret_rotation] %s", reason)
        return {"rotated": False, "reason": reason, "grace_days": 0, "previous_count": 0}

    grace = GRACE_PERIOD_DAYS if grace_days is None else max(0, int(grace_days))
    now = _utc_now()
    expires_at = now + timedelta(days=grace) if grace > 0 else now

    from src.db.session import SessionLocal
    from src.db.models import AppSettings

    db = SessionLocal()
    try:
        row = db.query(AppSettings).filter(AppSettings.key == JWT_SECRET_KEY).first()
        old_secret = row.value if row and row.value else None
        new_secret = secrets.token_hex(32)
        if row:
            row.value = new_secret
            row.description = "JWT签名密钥(轮换生成)"
        else:
            db.add(AppSettings(
                key=JWT_SECRET_KEY,
                value=new_secret,
                description="JWT签名密钥(轮换生成)",
            ))
        db.commit()
    finally:
        db.close()

    # 旧密钥入环(grace); 同步清理已过期条目
    records = get_previous_records()
    if old_secret:
        # 避免把同一 secret 重复压栈
        records = [r for r in records if r.get("secret") != old_secret]
        records.append({
            "secret": old_secret,
            "rotated_at": _iso(now),
            "expires_at": _iso(expires_at),
        })
    # 硬上限: 最多保留 5 个历史密钥(防环无限膨胀)
    if len(records) > 5:
        records = records[-5:]
    if grace > 0:
        _save_previous_records(records)
    else:
        _save_previous_records([])

    _set_rotated_at(now)
    invalidate_jwt_secret_cache()
    _invalidate_grace_cache()

    detail = (
        f"JWT_SECRET 已轮换 actor={actor}, grace={grace}d, "
        f"旧密钥可验证至 {_iso(expires_at) if grace > 0 else '(立即失效)'}"
    )
    try:
        from src.web.api.audit import log_audit
        log_audit(db=None, user=None, action="rotate_jwt_secret", detail=detail, ip="")
    except Exception as e:  # noqa: BLE001
        logger.warning("[secret_rotation] 审计写入失败: %s", e)

    logger.info("[secret_rotation] %s", detail)
    return {
        "rotated": True,
        "reason": "ok",
        "grace_days": grace,
        "previous_count": len(records) if grace > 0 else 0,
        "rotated_at": _iso(now),
        "grace_expires_at": _iso(expires_at) if grace > 0 else None,
    }


def maybe_auto_rotate(force: bool = False) -> dict | None:
    """到期自动轮换; 未到期返回 None。"""
    if _env_secret_pinned():
        logger.debug("[secret_rotation] env JWT_SECRET pinned, 自动轮换跳过")
        return None
    if not force:
        last = get_last_rotated_at()
        if last is not None:
            elapsed_days = (_utc_now() - last).total_seconds() / 86400.0
            if elapsed_days < ROTATION_INTERVAL_DAYS:
                return None
    try:
        return rotate_jwt_secret(actor="scheduler")
    except Exception as e:  # noqa: BLE001
        logger.error("[secret_rotation] 自动轮换失败: %s", e)
        return None


def _auto_rotate_job() -> None:
    """APScheduler 同步入口。"""
    try:
        maybe_auto_rotate(force=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("[secret_rotation] 轮换 job 异常: %s", e)


def get_rotation_status() -> dict:
    """供 admin 状态查询(不含 secret 明文)。"""
    last = get_last_rotated_at()
    prev = get_previous_records()
    return {
        "interval_days": ROTATION_INTERVAL_DAYS,
        "grace_days": GRACE_PERIOD_DAYS,
        "last_rotated_at": _iso(last) if last else None,
        "days_until_next": days_until_next_rotation(),
        "grace_secret_count": len(prev),
        "env_pinned": _env_secret_pinned(),
        "grace_expires_at": [r.get("expires_at") for r in prev],
    }


def register_rotation_job(scheduler) -> bool:
    """把每 90 天 JWT 密钥轮换 job 注册到**传入的现有** APScheduler 实例。

    模式对齐 auction_pool.register_cron / alerting.register_disk_check_job:
    复用现有调度器, 禁止新开; 注册时顺带检查一次是否到期(进程重启后不空转 90 天)。
    """
    if scheduler is None or not hasattr(scheduler, "add_job"):
        return False

    try:
        from apscheduler.triggers.interval import IntervalTrigger

        try:
            from src.core.timezone import _get_app_tz
            tz = _get_app_tz()
        except Exception:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Asia/Shanghai")

        scheduler.add_job(
            _auto_rotate_job,
            IntervalTrigger(days=ROTATION_INTERVAL_DAYS, timezone=tz),
            id=JOB_ID,
            name="JWT密钥轮换",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "[secret_rotation] 已注册 JWT 密钥轮换 job: 每 %d 天 (grace %d 天)",
            ROTATION_INTERVAL_DAYS, GRACE_PERIOD_DAYS,
        )
        # 进程启动时补一次到期检查(距上次轮换已超 90 天则立即轮换)
        try:
            maybe_auto_rotate(force=False)
        except Exception as e:  # noqa: BLE001
            logger.warning("[secret_rotation] 启动到期检查失败: %s", e)
        return True
    except Exception as e:  # noqa: BLE001
        logger.error("[secret_rotation] 轮换 job 注册失败: %s", e)
        return False
