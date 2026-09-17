"""数据库引擎与会话入口(薄壳, W3.1/D2; KI-039 切片 A 后再薄一层;
P1 读写分离 2026-09-18):

- src/db/dialect.py: 环境解析(DATA_DIR/DOCKER 门禁)/方言判定/引擎构造/
  SQLite 写锁/语义化 SQL 助手/读写分离 URL 解析
- src/db/session.py: **Base / engine / write_engine / read_engine /
  SessionLocal / get_db**(中立层, core 可直接用)
- src/db/backup.py: 版本化迁移前备份(sqlite 整库 + PG schema 快照)
- src/web/migrations.py: 版本化迁移唯一入口(B 层)
- 本模块: init_db() + 历史 import 面 re-export

读写分离(P1):
- `DATABASE_URL_WRITE` 主库; `DATABASE_URL_READ` 只读副本(可选)
- 未配置副本时 read_engine is write_engine, 行为与单库完全一致
- SessionLocal 自动 SELECT→读库 / DML→写库; `engine` 仍指向写库

历史 import 面保持不变: ``from src.web.database import engine/SessionLocal/
Base/get_db/DB_URL/DB_PATH/acquire_write`` 仍可用(新代码请直接 import
``src.db.session`` / ``src.db.dialect``)。
"""

from src.db.backup import backup_db_before_migration, backup_pg_schema_before_migration
from src.db.dialect import (  # noqa: F401
    DB_PATH,
    DB_URL,
    DATABASE_URL_READ,
    DATABASE_URL_WRITE,
    acquire_write,
    has_read_replica,
    read_db_url,
)
from src.db.session import (  # noqa: F401
    Base,
    ReadSessionLocal,
    RoutingSession,
    SessionLocal,
    WriteSessionLocal,
    engine,
    get_db,
    get_read_engine,
    get_write_engine,
    read_engine,
    write_engine,
)
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

    # 建表/迁移永远走写库(主库)
    Base.metadata.create_all(bind=engine)
    if has_pending_migrations(engine):
        backup_db_before_migration()
        backup_pg_schema_before_migration()
    run_versioned_migrations(engine)
