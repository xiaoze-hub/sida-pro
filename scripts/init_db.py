#!/usr/bin/env python
"""SIDA / PanWatch 一键初始化数据库 (2026-08-31 新增).

解决: 仓库无独立初始化脚本 (alembic init / setup_db / migrations.sh 都无),
新克隆者第一次跑不动。脚本直接调用 src.web.database.init_db(), 由其负责:
  1. 触发 SQLAlchemy ORM 类注册 (延迟 import src.web.models 避免循环依赖)
  2. Base.metadata.create_all() 创建所有未存在的表 (含 users 等)
  3. _migrate* 老式迁移 (兼容旧库)
  4. run_versioned_migrations() 跑 m101..m125 增量迁移
  5. 跑完打印 pending=0 即视为 OK

用法:
  python scripts/init_db.py              # 默认连 SQLite ./data/panwatch.db
  SIDA_DB_URL=postgresql+psycopg2://... python scripts/init_db.py
  python scripts/init_db.py --print-db   # 只打印当前 DB URL, 不动

设计要点:
  - 双方言兼容(SQLite/PG)复用 src.web.database 的 engine
  - 幂等: 已建表跳过, 已应用迁移跳过
  - 不引入新依赖 (alembic/sqlite3-diff 等)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让 `python scripts/init_db.py` 在仓库根目录能 import src.*
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="SIDA/PanWatch DB 一键初始化")
    parser.add_argument("--print-db", action="store_true", help="只打印当前 DB URL 然后退出")
    args = parser.parse_args()

    # 触发 Base.metadata 注册 (models 里所有 Table 全部挂上)
    from src.web.database import Base, DB_URL, engine, IS_PG, init_db  # noqa: F401

    if args.print_db:
        print(f"[init_db] DB_URL = {DB_URL}")
        print(f"[init_db] dialect = {'postgres' if IS_PG else 'sqlite'}")
        print(f"[init_db] registered tables = {len(Base.metadata.tables)}")
        return 0

    print(f"[init_db] engine = {DB_URL}")
    print(f"[init_db] dialect = {'postgres' if IS_PG else 'sqlite'}")
    print(f"[init_db] registered tables = {len(Base.metadata.tables)}")

    print("[init_db] step 1/2: init_db() = create_all + legacy migrate + versioned migrate ...")
    init_db()
    print(f"[init_db]   OK, current tables = {len(Base.metadata.tables)}")

    from src.web.migrations import has_pending_migrations

    print("[init_db] step 2/2: pending check ...")
    if has_pending_migrations(engine):
        print("[init_db] WARNING: 仍有 pending migrations, 手动重跑或查看 logs")
        return 2
    print("[init_db]   OK, all migrations applied")
    print("[init_db] DONE ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())