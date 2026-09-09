"""数据库方言层唯一入口: 环境解析 / 方言判定 / 引擎构造 / 语义化 SQL 助手。

W3.1(D2, 2026-09-09): 此前 IS_PG 模块级布尔散落 6 个文件 19 处, 业务代码各自
手写 PG/SQLite SQL 分叉。收编后:
- ``IS_PG`` 只允许出现在本文件(CI 门禁 scripts/check_is_pg_scope.py);
- 业务代码一律走 is_postgres() / declared_backend() / upsert_sql() /
  insert_ignore_sql() 语义化接口;
- 引擎构造(pool 参数/连接 PRAGMA)集中在 build_engine()。
"""

from __future__ import annotations

import os
import threading as _threading
from collections.abc import Sequence

from sqlalchemy import create_engine, event
from sqlalchemy.pool import NullPool

# ── 环境解析 ──────────────────────────────────────────────────────────────
# 数据库连接(2026-09-08: PG 为唯一生产口径, SQLite 仅本地开发/单测)
# - 容器内(DOCKER=1)无 SIDA_DB_URL 直接 fail-fast, 禁止静默落 sqlite
#   (历史教训: env 丢失 → 生产跑在容器内 sqlite → database is locked + 数据丢)
#   唯一逃生口: 显式 SIDA_ALLOW_SQLITE=1(仅本地开发/测试容器)
# - 本地开发(DOCKER 未设)默认 data/panwatch.db, 显式 SIDA_DB_URL 可切 PG
# - 例: SIDA_DB_URL="postgresql+psycopg2://sida:xxx@panwatch-postgres:5432/sida"
# W2.2/E4 (2026-09-09): DATA_DIR 是数据根的唯一口径 —— 设了 DATA_DIR(容器/
# 测试隔离), 默认库文件必须跟着进去。否则 delenv SIDA_DB_URL + reload 的测试
# 路径会解析回仓库 data/panwatch.db(实测测试曾把迁移跑进真实库, 留下 7 个 .bak)。
if os.environ.get("DATA_DIR"):
    DB_PATH = os.path.join(os.path.abspath(os.environ["DATA_DIR"]), "panwatch.db")
else:
    DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "panwatch.db")

if (
    os.environ.get("DOCKER") == "1"
    and not os.environ.get("SIDA_DB_URL")
    and os.environ.get("SIDA_ALLOW_SQLITE") != "1"
):
    raise RuntimeError(
        "DOCKER=1 但未设置 SIDA_DB_URL: 容器内禁止默认 SQLite,"
        "请在 compose/deploy 中注入 postgresql 连接串;"
        "如确需容器内 SQLite(仅本地开发/测试), 显式设 SIDA_ALLOW_SQLITE=1"
    )

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

DB_URL = os.environ.get("SIDA_DB_URL", f"sqlite:///{DB_PATH}")

IS_PG = DB_URL.startswith("postgresql")


def is_postgres() -> bool:
    """业务代码判定后端一律用本函数(禁直接 import IS_PG, CI 门禁)。"""
    return IS_PG


def declared_backend() -> str:
    """方言标签(sqlite/postgresql), 健康检查/日志展示用。"""
    return "postgresql" if IS_PG else "sqlite"


# ── 引擎构造 ──────────────────────────────────────────────────────────────
def build_engine(db_url: str | None = None):
    """按方言构造引擎: PG 连接池参数与 SQLite PRAGMA 集中在此。"""
    url = db_url or DB_URL
    pg = url.startswith("postgresql")
    if pg:
        eng = create_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            # 修复 2026-08-21: 调大连接池, 实测 size=5 + overflow=10 在 26 并发下被打满,
            # 触发 sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached
            # 调为 10 + 20 (30 总上限) 应对 Dashboard 一次刷新 26 API 并发
            # 2026-09-03 v0.4.78: 10+20(30)/30s 还是不够, abnormal_moves 单接口扫自选+候选池
            # 每只股 analyze_for_symbols 内调 K线 → 多并发 30s 内打满 → QueuePool TimeoutError
            # → 接口 30s 超时撞 502。提到 20+40(60 总) + 超时 30→10s, 让失败快速暴露
            # 而不阻塞 30s; 同时 GET 路径 summary_cache 缓存已落库(v0.4.77), 不会因缓存
            # 失败就全堆在 DB 上。
            pool_size=20,
            max_overflow=40,
            pool_timeout=10,  # v0.4.78: 30→10s, 失败快速失败
            pool_recycle=1800,  # 30min 回收, 防止 PG 端 idle in transaction
        )
    else:
        eng = create_engine(
            url,
            echo=False,
            connect_args={
                "timeout": 30,
                "check_same_thread": False,
            },
            poolclass=NullPool,
        )

    @event.listens_for(eng, "connect")
    def _set_db_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        if pg:
            cursor.execute("SET statement_timeout = 8000")  # v0.4.78: 30s→8s, 配合 pool_timeout=10s 让慢查询快速失败
        else:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            # 积极 checkpoint: 减少 WAL 膨胀, 降低锁竞争窗口
            cursor.execute("PRAGMA wal_autocheckpoint=1000")
        cursor.close()

    return eng


# ── SQLite 写锁信号量(2026-08-11) ────────────────────────────────────────
# WAL 模式读写不互斥, 但写-写互斥。多用户后写请求变多(订阅推送/定时任务),
# 并发写会互相等待超时 → database is locked。用信号量把并发写限制为 1,
# 排队而非冲突, 根治锁死。
_sqlite_write_lock = _threading.Semaphore(1)


def acquire_write():
    """写操作前调用: SQLite 排队等写锁(最多30s); PG 模式 MVCC 行级锁, 无需排队。"""
    if is_postgres():
        class _Noop:
            def release(self):
                pass

        return _Noop()
    acquired = _sqlite_write_lock.acquire(timeout=30)
    if not acquired:
        raise TimeoutError("数据库写入繁忙, 请稍后重试")
    return _sqlite_write_lock


# ── 语义化 SQL 助手 ──────────────────────────────────────────────────────
def upsert_sql(
    table: str,
    insert_cols: Sequence[str],
    conflict_cols: Sequence[str],
    update_cols: Sequence[str],
) -> str:
    """双方言 UPSERT: 冲突时按 update_cols 覆盖。

    SQLite 3.24+ 与 PG 都支持 ``ON CONFLICT (...) DO UPDATE SET x = excluded.x``
    (unquoted 标识符在 PG 折叠小写, SQLite 关键字大小写不敏感)。
    """
    cols = ", ".join(insert_cols)
    vals = ", ".join(f":{c}" for c in insert_cols)
    key = ", ".join(conflict_cols)
    sets = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
    return (
        f"INSERT INTO {table} ({cols}) VALUES ({vals}) "
        f"ON CONFLICT ({key}) DO UPDATE SET {sets}"
    )


def insert_ignore_sql(table: str, insert_cols: Sequence[str]) -> str:
    """双方言"冲突即跳过"插入: PG 走 ON CONFLICT DO NOTHING, SQLite 走 OR IGNORE。"""
    cols = ", ".join(insert_cols)
    vals = ", ".join(f":{c}" for c in insert_cols)
    if is_postgres():
        return f"INSERT INTO {table} ({cols}) VALUES ({vals}) ON CONFLICT DO NOTHING"
    return f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({vals})"
