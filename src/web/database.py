"""数据库引擎与会话入口(薄壳, W3.1/D2; KI-039 切片 A 后再薄一层):

- src/db/dialect.py: 环境解析(DATA_DIR/DOCKER 门禁)/方言判定/引擎构造/
  SQLite 写锁/语义化 SQL 助手
- src/db/session.py: **Base / engine / SessionLocal / get_db**(中立层, core 可直接用)
- src/db/backup.py: 版本化迁移前备份(sqlite 整库 + PG schema 快照)
- src/web/migrations.py: 版本化迁移唯一入口(B 层)
- 本模块: init_db() + 历史 import 面 re-export

历史 import 面保持不变: ``from src.web.database import engine/SessionLocal/
Base/get_db/DB_URL/DB_PATH/acquire_write`` 仍可用(新代码请直接 import
``src.db.session`` / ``src.db.dialect``)。
"""

from src.db.backup import backup_db_before_migration, backup_pg_schema_before_migration
from src.db.dialect import DB_PATH, DB_URL, acquire_write  # noqa: F401
from src.db.session import Base, SessionLocal, engine, get_db  # noqa: F401
from src.web.migrations import has_pending_migrations, run_versioned_migrations
# src.web.models 在 init_db() 里延迟 import 以避开循环依赖
# (models 顶部 from src.db.session import Base)


def init_db():
    # 延迟 import: 触发所有 ORM 类注册到 Base.metadata (User/AuditLog/Account/...)
    # 否则 Base.metadata.create_all() 不知有这些表。
    # schema 变更分层(2026-09-08 W1.5/A5): create_all 只建缺表; 一切加列/建表/
    # 数据回填走 B 层 run_versioned_migrations(含 2026-09-09 收编的历史 A 层
    # legacy 迁移 143-148)。新增表/列一律写 B 层新版本。
    from src.web import models as _models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    if has_pending_migrations(engine):
        backup_db_before_migration()
        backup_pg_schema_before_migration()
    run_versioned_migrations(engine)
