"""版本化迁移前的备份(W3.1/D2 自 src/web/database.py 收编)。

- SQLite: 整库文件 copy2 → *.bak.<ts>
- PG: pg_dump --schema-only 结构快照(红线: 不在生产库跑 pg_restore, 整库
  dump 太重且恢复需要 pg_restore, 故只存 schema)
- 由 init_db() 在 has_pending_migrations() 为真时调用, 两个备份都尽力而为
  (fail-soft), 不阻断迁移。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from datetime import datetime

logger = logging.getLogger(__name__)


def backup_db_before_migration() -> None:
    """Create a timestamped sqlite backup before versioned migrations."""
    from src.db import dialect as _dialect

    db_path = _dialect.DB_PATH
    if not os.path.exists(db_path):
        return
    try:
        size = os.path.getsize(db_path)
        if size <= 0:
            return
    except Exception:
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{db_path}.bak.{ts}"
    try:
        shutil.copy2(db_path, backup_path)
        logger.info(f"数据库迁移前备份已创建: {backup_path}")
    except Exception as e:
        logger.warning(f"数据库迁移前备份失败: {e}")


def backup_pg_schema_before_migration() -> None:
    """PG 迁移前用 pg_dump --schema-only 留一份结构快照(W1.5/A5)。

    pg_dump 二进制不在应用容器内时 fail-soft 跳过(警告日志), 不阻断迁移。
    """
    from src.db import dialect as _dialect

    if not _dialect.is_postgres():
        return
    from sqlalchemy.engine import make_url

    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        # KI-014: 无 pg_dump 时不再只打日志跳过 —— 用 SQLAlchemy inspector 写一份
        # 表/列清单(非完整 DDL, 但比“静默什么都不留”强), 仍 fail-soft。
        logger.warning(
            "pg_dump 不在 PATH —— 改用 SQLAlchemy inspector 写表列清单(KI-014 降级路径)"
        )
        _schema_fallback_via_inspector()
        return
    backup_dir = os.path.join(
        os.path.dirname(os.path.abspath(_dialect.DB_PATH)), "migrations_backup"
    )
    os.makedirs(backup_dir, exist_ok=True)
    out = os.path.join(
        backup_dir, f"schema_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"
    )
    url = make_url(_dialect.DB_URL)
    cmd = [
        pg_dump,
        "--schema-only",
        "--no-owner",
        "--no-privileges",
        "-h",
        str(url.host or "localhost"),
        "-p",
        str(url.port or 5432),
        "-U",
        str(url.username or "sida"),
        "-d",
        str(url.database or "sida"),
        "-f",
        out,
    ]
    env = dict(os.environ)
    if url.password:
        # 密码只经环境变量传递, 绝不进命令行参数/日志
        env["PGPASSWORD"] = url.password
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, timeout=120)
        if proc.returncode == 0:
            logger.info("PG 迁移前 schema 快照已写入 %s", out)
        else:
            logger.warning(
                "pg_dump 失败(rc=%s): %s",
                proc.returncode,
                proc.stderr.decode("utf-8", "replace")[:500],
            )
    except Exception as e:  # noqa: BLE001 — 快照是尽力而为, 不阻断迁移
        logger.warning("pg_dump schema 快照失败(fail-soft): %s", e)


def _schema_fallback_via_inspector() -> None:
    """KI-014 降级: 无 pg_dump 时用 inspector 落表/列清单, 供人工比对迁移前后。"""
    try:
        from src.db import dialect as _dialect
        from src.db.session import engine
        from sqlalchemy import inspect as sa_inspect
    except Exception as e:  # noqa: BLE001
        logger.warning("inspector 降级快照不可用: %s", e)
        return
    backup_dir = os.path.join(
        os.path.dirname(os.path.abspath(_dialect.DB_PATH)), "migrations_backup"
    )
    os.makedirs(backup_dir, exist_ok=True)
    out = os.path.join(
        backup_dir, f"schema_inspect_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    )
    try:
        insp = sa_inspect(engine)
        lines = [f"# schema inspect fallback @ {datetime.now().isoformat(timespec='seconds')}"]
        for table in sorted(insp.get_table_names()):
            cols = insp.get_columns(table)
            col_desc = ", ".join(f"{c['name']}:{c['type']}" for c in cols)
            lines.append(f"TABLE {table} ({col_desc})")
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info("PG 迁移前 inspector 清单已写入 %s (%d 表)", out, len(lines) - 1)
    except Exception as e:  # noqa: BLE001
        logger.warning("inspector schema 清单失败(fail-soft): %s", e)
