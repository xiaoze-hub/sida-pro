"""数据库自动备份/恢复核心 (2026-09-18 tier1-compliance)。

- 备份: pg_dump(SQLite 则文件复制) → gzip → DATA_DIR/backups/
- 保留: 默认 30 天, 自动清理过期文件
- 校验: 写完后 gzip 完整性校验(可完整解压读出)
- 失败: 调 AlertManager 发 backup_failed 告警
- 定时: register_daily_backup_job 注册每日 03:00 APScheduler job

CLI 入口见 scripts/backup_auto.py / scripts/restore_backup.py。
"""

from __future__ import annotations

import glob
import gzip
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "backup_"
BACKUP_SUFFIX = ".sql.gz"
DEFAULT_RETENTION_DAYS = 30


def get_data_dir() -> str:
    env = (os.environ.get("DATA_DIR") or "").strip()
    if env:
        return env
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    return os.path.join(root, "data")


def get_backup_dir() -> str:
    return os.path.join(get_data_dir(), "backups")


@dataclass
class BackupResult:
    ok: bool
    path: str = ""
    size_bytes: int = 0
    duration_ms: int = 0
    error: str = ""
    dialect: str = ""
    cleaned: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "dialect": self.dialect,
            "cleaned": self.cleaned,
        }


def _db_url() -> str:
    try:
        from src.db.dialect import DB_URL

        return DB_URL or ""
    except Exception:
        return os.environ.get("SIDA_DB_URL") or ""


def _is_postgres(url: str | None = None) -> bool:
    u = url if url is not None else _db_url()
    return bool(u) and u.startswith("postgresql")


def _pg_dump_bin() -> str | None:
    return shutil.which("pg_dump")


def _backup_filename(ts: datetime | None = None) -> str:
    ts = ts or datetime.now()
    # 微秒防同秒冲突(恢复前 pre-backup 与原备份可能落在同一秒)
    return f"{BACKUP_PREFIX}{ts.strftime('%Y%m%d_%H%M%S')}_{ts.microsecond:06d}{BACKUP_SUFFIX}"


def verify_gzip_file(path: str) -> bool:
    """校验 gzip 完整性: 能完整解压读出任意字节即视为有效。"""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return False
        with gzip.open(path, "rb") as f:
            while f.read(1024 * 1024):
                pass
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("[backup] gzip 校验失败 %s: %s", path, e)
        return False


def list_backups(backup_dir: str | None = None) -> list[dict[str, Any]]:
    """列出可用备份(按时间倒序)。"""
    d = backup_dir or get_backup_dir()
    if not os.path.isdir(d):
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(glob.glob(os.path.join(d, BACKUP_PREFIX + "*" + BACKUP_SUFFIX)), reverse=True):
        try:
            st = os.stat(p)
            name = os.path.basename(p)
            ts_part = name[len(BACKUP_PREFIX):-len(BACKUP_SUFFIX)]
            # 只回日期_时间核心段, 微秒段可选
            core = "_".join(ts_part.split("_")[:2]) if "_" in ts_part else ts_part
            out.append({
                "path": p,
                "name": name,
                "size_bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "ts": core,
            })
        except Exception:
            continue
    return out


def cleanup_old_backups(
    backup_dir: str | None = None,
    retention_days: int | None = None,
) -> int:
    """删除超过保留天数的备份, 返回删除数量。"""
    d = backup_dir or get_backup_dir()
    days = retention_days if retention_days is not None else _env_int(
        "BACKUP_RETENTION_DAYS", DEFAULT_RETENTION_DAYS
    )
    if days <= 0 or not os.path.isdir(d):
        return 0
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    for item in list_backups(d):
        try:
            name = item["name"]
            # backup_YYYYMMDD_HHMMSS[_ffffff].sql.gz
            ts_part = name[len(BACKUP_PREFIX):-len(BACKUP_SUFFIX)]
            ts_core = ts_part.split("_", 3)
            # 取日期_时间两段; 微秒段可有可无
            date_time = f"{ts_core[0]}_{ts_core[1]}" if len(ts_core) >= 2 else ts_part
            file_dt = datetime.strptime(date_time, "%Y%m%d_%H%M%S")
            if file_dt < cutoff:
                os.remove(item["path"])
                removed += 1
                logger.info("[backup] 清理过期备份: %s", name)
        except Exception as e:  # noqa: BLE001
            logger.warning("[backup] 清理失败 %s: %s", item.get("name"), e)
    return removed


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip())
    except Exception:
        return default


def _notify_backup_failed(detail: str) -> None:
    try:
        from src.core.alerting import get_alert_manager

        get_alert_manager().send_backup_failed(detail)
    except Exception as e:  # noqa: BLE001
        logger.warning("[backup] 失败告警发送异常(忽略): %s", e)


def _dump_postgres(out_path: str, url: str) -> None:
    """pg_dump 流式写入 gzip 文件。密码只经 PGPASSWORD, 不进命令行。"""
    from sqlalchemy.engine import make_url

    pg_dump = _pg_dump_bin()
    if not pg_dump:
        raise RuntimeError("pg_dump 不在 PATH, 无法执行 PG 备份")

    parsed = make_url(url)
    # pg_dump 默认输出 plain SQL; 再由本函数 gzip
    cmd = [
        pg_dump,
        "--no-owner",
        "--no-privileges",
        "-h", str(parsed.host or "localhost"),
        "-p", str(parsed.port or 5432),
        "-U", str(parsed.username or "sida"),
        "-d", str(parsed.database or "sida"),
    ]
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password

    with open(out_path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            assert proc.stdout is not None
            try:
                for chunk in iter(lambda: proc.stdout.read(1024 * 64), b""):
                    gz.write(chunk)
            finally:
                proc.stdout.close()
            stderr = proc.stderr.read() if proc.stderr else b""
            if proc.stderr:
                proc.stderr.close()
            rc = proc.wait(timeout=600)
            if rc != 0:
                raise RuntimeError(
                    f"pg_dump 失败(rc={rc}): {stderr.decode('utf-8', 'replace')[:500]}"
                )


def _dump_sqlite(out_path: str, url: str) -> None:
    """SQLite: 复制库文件并 gzip(开发/单测环境)。"""
    path = _sqlite_path_from_url(url)
    if not path or not os.path.isfile(path):
        # 回落 dialect.DB_PATH
        try:
            from src.db import dialect as _d

            path = _d.DB_PATH
        except Exception:
            pass
    if not path or not os.path.isfile(path):
        raise RuntimeError(f"SQLite 库文件不存在: {path or url}")

    with open(path, "rb") as src, open(out_path, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
            for chunk in iter(lambda: src.read(1024 * 64), b""):
                gz.write(chunk)


def run_backup(
    backup_dir: str | None = None,
    retention_days: int | None = None,
    notify_on_failure: bool = True,
) -> BackupResult:
    """执行一次完整备份: dump → gzip 校验 → 清理旧档。失败时告警。"""
    t0 = time.perf_counter()
    url = _db_url()
    dialect = "postgresql" if _is_postgres(url) else "sqlite"
    out_dir = backup_dir or get_backup_dir()
    result = BackupResult(ok=False, dialect=dialect)

    try:
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, _backup_filename())
        # 先写 .tmp, 校验通过后原子 rename, 避免半截文件被 restore 误用
        tmp_path = out_path + ".tmp"

        if dialect == "postgresql":
            _dump_postgres(tmp_path, url)
        else:
            _dump_sqlite(tmp_path, url)

        if not verify_gzip_file(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise RuntimeError("gzip 完整性校验失败, 已丢弃临时文件")

        os.replace(tmp_path, out_path)
        size = os.path.getsize(out_path)
        result.ok = True
        result.path = out_path
        result.size_bytes = size
        logger.info(
            "[backup] 备份完成: %s (%.1f KB, %s)",
            out_path,
            size / 1024.0,
            dialect,
        )
    except Exception as e:  # noqa: BLE001
        result.error = str(e)
        logger.error("[backup] 备份失败: %s", e)
        if notify_on_failure:
            _notify_backup_failed(str(e))

    result.duration_ms = int((time.perf_counter() - t0) * 1000)
    try:
        result.cleaned = cleanup_old_backups(out_dir, retention_days)
    except Exception as e:  # noqa: BLE001
        logger.warning("[backup] 清理旧备份异常: %s", e)
    return result


def restore_backup(
    backup_path: str,
    *,
    pre_backup: bool = True,
    db_url: str | None = None,
) -> dict[str, Any]:
    """从备份文件恢复。

    - 恢复前默认先做一次当前库备份(pre_backup)。
    - PG: gunzip 流式灌 psql。
    - SQLite: 解压后覆盖 DB_PATH(先备份原文件)。
    """
    if not os.path.isfile(backup_path):
        return {"ok": False, "error": f"备份文件不存在: {backup_path}"}
    if not verify_gzip_file(backup_path):
        return {"ok": False, "error": f"备份文件 gzip 校验失败: {backup_path}"}

    url = db_url or _db_url()
    dialect = "postgresql" if _is_postgres(url) else "sqlite"
    pre_path = ""

    try:
        if pre_backup:
            pre = run_backup(notify_on_failure=False)
            if not pre.ok:
                return {
                    "ok": False,
                    "error": f"恢复前备份失败, 已中止恢复: {pre.error}",
                    "pre_backup": pre.to_dict(),
                }
            pre_path = pre.path

        if dialect == "postgresql":
            _restore_postgres(backup_path, url)
        else:
            _restore_sqlite(backup_path, url)

        logger.info("[backup] 恢复完成: %s → %s", backup_path, dialect)
        return {
            "ok": True,
            "path": backup_path,
            "dialect": dialect,
            "pre_backup_path": pre_path,
        }
    except Exception as e:  # noqa: BLE001
        logger.error("[backup] 恢复失败: %s", e)
        return {
            "ok": False,
            "error": str(e),
            "path": backup_path,
            "dialect": dialect,
            "pre_backup_path": pre_path,
        }


def _restore_postgres(backup_path: str, url: str) -> None:
    from sqlalchemy.engine import make_url

    psql = shutil.which("psql")
    if not psql:
        raise RuntimeError("psql 不在 PATH, 无法恢复 PG 备份")

    parsed = make_url(url)
    cmd = [
        psql,
        "-h", str(parsed.host or "localhost"),
        "-p", str(parsed.port or 5432),
        "-U", str(parsed.username or "sida"),
        "-d", str(parsed.database or "sida"),
        "-v", "ON_ERROR_STOP=1",
        "-q",
    ]
    env = dict(os.environ)
    if parsed.password:
        env["PGPASSWORD"] = parsed.password

    with gzip.open(backup_path, "rb") as gz:
        proc = subprocess.run(
            cmd,
            stdin=gz,
            capture_output=True,
            env=env,
            timeout=1800,
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"psql 恢复失败(rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', 'replace')[:800]}"
        )


def _sqlite_path_from_url(url: str) -> str:
    """从 sqlite URL 解析库文件路径; 解析不到返回空串。"""
    if not url:
        return ""
    if url.startswith("sqlite:///"):
        # sqlite:///relative 或 sqlite:////abs (四个斜杠)
        return url[len("sqlite:///"):]
    return ""


def _restore_sqlite(backup_path: str, url: str) -> None:
    # 优先用 URL 指向的库(测试/显式 SIDA_DB_URL); 否则回落 dialect.DB_PATH
    db_path = _sqlite_path_from_url(url)
    if not db_path:
        try:
            from src.db import dialect as _d

            db_path = _d.DB_PATH
        except Exception:
            db_path = ""

    if not db_path:
        raise RuntimeError("无法解析 SQLite 目标路径")

    # 再留一份文件级快照(与 run_backup 的 sql.gz 并存)
    if os.path.isfile(db_path):
        snap = f"{db_path}.bak.pre_restore.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(db_path, snap)
        logger.info("[backup] SQLite 恢复前文件快照: %s", snap)

    tmp = db_path + ".restore_tmp"
    with gzip.open(backup_path, "rb") as gz, open(tmp, "wb") as out:
        for chunk in iter(lambda: gz.read(1024 * 64), b""):
            out.write(chunk)
    os.replace(tmp, db_path)


# ── APScheduler: 每日凌晨 3 点 ──
_backup_job_lock = threading.Lock()
_backup_job_running = False


def _daily_backup_job() -> None:
    """APScheduler 同步入口: 串行执行, 防止重叠。"""
    global _backup_job_running
    with _backup_job_lock:
        if _backup_job_running:
            logger.warning("[backup] 上一轮备份仍在执行, 本轮跳过")
            return
        _backup_job_running = True
    try:
        result = run_backup()
        if result.ok:
            logger.info(
                "[backup] 每日备份完成: %s (%.1f KB)",
                result.path,
                result.size_bytes / 1024.0,
            )
        # 失败告警已在 run_backup 内处理
    finally:
        with _backup_job_lock:
            _backup_job_running = False


def register_daily_backup_job(scheduler=None, hour: int = 3, minute: int = 0):
    """注册每日凌晨备份 job(默认 03:00 本地时区)。

    Args:
        scheduler: 已有 APScheduler 实例则复用。
        hour/minute: 触发时刻(默认 3 点)。
    """
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    try:
        from src.core.timezone import _get_app_tz

        tz = _get_app_tz()
    except Exception:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Asia/Shanghai")

    sched = scheduler if scheduler is not None else BackgroundScheduler(timezone=tz)
    sched.add_job(
        _daily_backup_job,
        CronTrigger(hour=hour, minute=minute, timezone=tz),
        id="daily_db_backup",
        name="每日数据库自动备份",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    logger.info("[backup] 已注册每日 %02d:%02d 数据库备份 job", hour, minute)
    return sched
