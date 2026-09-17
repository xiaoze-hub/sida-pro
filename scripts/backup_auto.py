#!/usr/bin/env python
"""数据库自动备份 CLI (2026-09-18 tier1-compliance)。

用法:
  python scripts/backup_auto.py                 # 立即备份一次
  python scripts/backup_auto.py --list          # 列出可用备份
  python scripts/backup_auto.py --cleanup-only  # 只清理过期备份
  python scripts/backup_auto.py --retention-days 14

逻辑实现在 src/core/db_backup_auto.py:
  - PG: pg_dump → gzip → DATA_DIR/backups/backup_YYYYMMDD_HHMMSS.sql.gz
  - SQLite: 库文件复制 → gzip
  - gzip 完整性校验; 失败发 backup_failed 告警
  - 默认保留 30 天
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("backup_auto")


def main() -> int:
    parser = argparse.ArgumentParser(description="SIDA/PanWatch 数据库自动备份")
    parser.add_argument("--list", action="store_true", help="列出可用备份后退出")
    parser.add_argument("--cleanup-only", action="store_true", help="只清理过期备份")
    parser.add_argument("--retention-days", type=int, default=None, help="保留天数(默认 30)")
    parser.add_argument("--dir", default=None, help="备份目录(默认 DATA_DIR/backups)")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    from src.core.db_backup_auto import (
        cleanup_old_backups,
        get_backup_dir,
        list_backups,
        run_backup,
    )

    if args.list:
        items = list_backups(args.dir)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            if not items:
                print(f"(无备份) 目录: {args.dir or get_backup_dir()}")
            for it in items:
                print(f"{it['name']}  {it['size_bytes'] / 1024:.1f} KB  {it['mtime']}")
        return 0

    if args.cleanup_only:
        n = cleanup_old_backups(args.dir, args.retention_days)
        logger.info("已清理 %d 个过期备份", n)
        return 0

    result = run_backup(backup_dir=args.dir, retention_days=args.retention_days)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        if result.ok:
            logger.info(
                "备份成功: %s (%.1f KB, 耗时 %d ms, 清理 %d 个)",
                result.path,
                result.size_bytes / 1024.0,
                result.duration_ms,
                result.cleaned,
            )
        else:
            logger.error("备份失败: %s", result.error)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
