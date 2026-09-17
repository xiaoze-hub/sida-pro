#!/usr/bin/env python
"""数据库恢复 CLI (2026-09-18 tier1-compliance)。

用法:
  python scripts/restore_backup.py --list
  python scripts/restore_backup.py --file DATA_DIR/backups/backup_20260918_030000.sql.gz
  python scripts/restore_backup.py --latest
  python scripts/restore_backup.py --file ... --no-pre-backup   # 跳过恢复前备份(危险)

行为:
  - 恢复前自动备份当前库(默认开启), 备份失败则中止恢复
  - 校验目标备份 gzip 完整性
  - PG: gunzip 流式灌 psql; SQLite: 解压覆盖 DB_PATH
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
logger = logging.getLogger("restore_backup")


def main() -> int:
    parser = argparse.ArgumentParser(description="SIDA/PanWatch 数据库恢复")
    parser.add_argument("--list", action="store_true", help="列出可用备份后退出")
    parser.add_argument("--file", default=None, help="要恢复的备份文件路径")
    parser.add_argument("--latest", action="store_true", help="恢复最新备份")
    parser.add_argument(
        "--no-pre-backup",
        action="store_true",
        help="跳过恢复前自动备份(危险, 仅调试用)",
    )
    parser.add_argument("--dir", default=None, help="备份目录(默认 DATA_DIR/backups)")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = parser.parse_args()

    from src.core.db_backup_auto import (
        get_backup_dir,
        list_backups,
        restore_backup,
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

    target = args.file
    if args.latest:
        items = list_backups(args.dir)
        if not items:
            logger.error("没有可用备份")
            return 1
        target = items[0]["path"]

    if not target:
        parser.error("请指定 --file <path> 或 --latest")

    logger.info("准备恢复: %s (pre_backup=%s)", target, not args.no_pre_backup)
    result = restore_backup(target, pre_backup=not args.no_pre_backup)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result.get("ok"):
            logger.info(
                "恢复成功: %s (dialect=%s, pre_backup=%s)",
                result.get("path"),
                result.get("dialect"),
                result.get("pre_backup_path") or "(无)",
            )
        else:
            logger.error("恢复失败: %s", result.get("error"))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
