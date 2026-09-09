"""src/db — 数据库方言层(W3.1/D2, 2026-09-09)。

分工:
- dialect.py: 环境解析(DATA_DIR/DOCKER 门禁)/方言判定/引擎构造/SQLite 写锁/
  语义化 SQL 助手(upsert_sql / insert_ignore_sql)
- backup.py: 版本化迁移前的备份(sqlite 整库 + PG schema 快照)

红线: ``IS_PG`` 模块级布尔只允许出现在 dialect.py(CI 门禁
scripts/check_is_pg_scope.py)。业务代码一律走 is_postgres() /
declared_backend() / upsert_sql() / insert_ignore_sql()。
版本化迁移(B 层, 唯一 schema 变更入口)仍在 src/web/migrations.py,
其内部用连接级 conn.dialect.name 判定, 与本包无依赖。
"""
from src.db.dialect import (  # noqa: F401
    DB_PATH,
    DB_URL,
    build_engine,
    declared_backend,
    insert_ignore_sql,
    is_postgres,
    upsert_sql,
)
