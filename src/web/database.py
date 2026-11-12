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
    ReadOnlyBindError,
    RoutingSession,
    SessionLocal,
    WriteSessionLocal,
    engine,
    get_db,
    get_read_engine,
    get_write_db,
    get_write_engine,
    pin_write_bind,
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

    # 建表/迁移走**迁移角色**(2026-11-03 Phase 2): 运行时角色无 DDL 权限,
    # 未配置 DATABASE_URL_MIGRATE 时与写库同引擎, 行为不变。
    from src.db.dialect import migrate_engine

    mig = migrate_engine()
    Base.metadata.create_all(bind=mig)
    if has_pending_migrations(mig):
        backup_db_before_migration()
        backup_pg_schema_before_migration()
    run_versioned_migrations(mig)

    # 2026-11-03: `auth_session_revocations`(会话墓碑) 不在 ORM models 里, 靠
    # permissions._REVOCATION_DDL 幂等建表。角色拆分后运行时角色没有 CREATE 权限,
    # 必须在这里(迁移角色)建好, 否则「踢设备」的墓碑写入会静默失败。
    try:
        from sqlalchemy import inspect, text

        from src.core.permissions import _REVOCATION_DDL

        with mig.begin() as _conn:
            if not inspect(_conn).has_table("auth_session_revocations"):
                _conn.execute(text(_REVOCATION_DDL))
    except Exception as e:  # noqa: BLE001 - 建表失败不得阻断启动
        import logging

        logging.getLogger(__name__).warning("[db] 会话墓碑表建表跳过: %r", e)

    # 角色拆分下(2026-11-03 Phase 2)新表的属主是**迁移角色**, 不是原来的 owner,
    # 所以 ALTER DEFAULT PRIVILEGES 管不到它 —— 必须在每次启动后重新授一遍,
    # 否则任何新建表运行时角色都读不了(表现为发版后新功能一律 500/权限错误)。
    _sync_app_role_grants(mig)


def _sync_app_role_grants(mig) -> None:
    """把 public 下所有表的读写权授给运行时角色(仅角色拆分时执行)。"""
    import os
    import re

    from sqlalchemy import text

    role = (os.environ.get("SIDA_APP_DB_ROLE") or "").strip()
    if not role:
        return  # 未拆分部署: 运行时角色就是表属主, 不需要补授
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
        import logging

        logging.getLogger(__name__).warning("[db] SIDA_APP_DB_ROLE 非法, 跳过授权: %r", role)
        return
    try:
        with mig.begin() as conn:
            conn.execute(
                text(
                    f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{role}"'
                )
            )
            conn.execute(
                text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"')
            )
    except Exception as e:  # noqa: BLE001 - 授权失败不阻断启动, 但必须留痕
        import logging

        logging.getLogger(__name__).warning("[db] 运行时角色补授权失败: %r", e)
