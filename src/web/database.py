"""数据库引擎与会话入口(薄壳, W3.1/D2 重构后分工):

- src/db/dialect.py: 环境解析(DATA_DIR/DOCKER 门禁)/方言判定/引擎构造/
  SQLite 写锁/语义化 SQL 助手
- src/db/backup.py: 版本化迁移前备份(sqlite 整库 + PG schema 快照)
- src/web/migrations.py: 版本化迁移唯一入口(B 层; 原本模块内的历史 A 层
  _migrate* 已收编为版本 143-148, 见 _m143.._m148)
- 本模块: Base(ORM 元数据锚点, models.py 绑定于此) + engine/SessionLocal +
  init_db()

历史 import 面保持不变: ``from src.web.database import engine/SessionLocal/
Base/get_db/DB_URL/DB_PATH/acquire_write`` 仍可用(方言判定请改用
``from src.db.dialect import is_postgres``)。
"""

from sqlalchemy.orm import DeclarativeBase, sessionmaker

from src.db.backup import backup_db_before_migration, backup_pg_schema_before_migration
from src.db.dialect import DB_PATH, DB_URL, acquire_write, build_engine  # noqa: F401
from src.web.migrations import has_pending_migrations, run_versioned_migrations
# src.web.models 在 init_db() 里延迟 import 以避开循环依赖
# (models 顶部 from src.web.database import Base)


# W2.2/E4 reload 防御: importlib.reload(本模块)保留 module __dict__ —— 必须复用
# 旧 Base。若无条件重建, models.py 等在各自 import 时绑定了旧 Base 的 ORM 模型
# 会与 reload 后的新 Base 脱钩, Base.metadata.create_all 建出空库(no such table)。
# (test_pg_default 等用例按 DOCKER/env 语义 reload 本模块, 历史上靠字母序凑巧
# 排在受害者之后未暴露。)
if "Base" not in globals():

    class Base(DeclarativeBase):
        pass


engine = build_engine()

SessionLocal = sessionmaker(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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
