"""ORM 会话入口(中立层, KI-039 切片 A, 2026-09-09; P1 读写分离 2026-09-18)。

从 `src/web/database.py` 下沉而来 —— 核心逻辑(`src/core/*`)不应依赖 Web 层:

- `Base`: ORM 元数据锚点(models 绑定于此)
- `engine` / `write_engine` / `read_engine` / `SessionLocal` / `get_db`
- 迁移/备份/建表等 **Web 侧初始化** 仍留在 `src/web/database.py::init_db()`

读写分离(P1 2026-09-18):
- `DATABASE_URL_WRITE` 主库(必填, 缺省回落 SIDA_DB_URL / SQLite)
- `DATABASE_URL_READ` 只读副本(可选); **未配置时 read_engine is write_engine**,
  与历史单库行为完全一致(向后兼容)。
- `SessionLocal` 使用 RoutingSession: SELECT 走读库, INSERT/UPDATE/DELETE 走写库;
  事务/写锁路径自动回主库。
- `engine` 保持指向写库 —— 历史 `engine.begin()` / `create_all(bind=engine)` 不受影响。

兼容: `from src.web.database import Base/engine/SessionLocal/get_db` 继续可用
(那边是 re-export); `src/db` 内部与 core 请改用本模块。
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.db.dialect import (  # noqa: F401
    DB_PATH,
    DB_URL,
    acquire_write,
    build_engine,
    has_read_replica,
    is_read_clause,
    read_db_url,
)


# reload 防御(沿用 src/web/database 原注释): importlib.reload 保留 module
# __dict__, 必须复用旧 Base, 否则已绑定旧 Base 的 ORM 模型会与新建的
# Base.metadata 脱钩(create_all 建出空库)。
if "Base" not in globals():

    class Base(DeclarativeBase):
        pass


# ── 双引擎(读写分离) ────────────────────────────────────────────────────
# write_engine: 主库, 所有写 + 迁移 + create_all
# read_engine : 只读副本; 未配置 DATABASE_URL_READ 时与 write_engine 是同一对象
write_engine = build_engine()
engine = write_engine  # 历史 re-export 面: engine 永远是写库

if has_read_replica():
    read_engine = build_engine(read_db_url())
else:
    read_engine = write_engine


def get_read_engine():
    """只读引擎(无副本时返回写库, 行为与单库一致)。"""
    return read_engine


def get_write_engine():
    return write_engine


class RoutingSession(Session):
    """按 SQL 语句路由到读/写引擎的 Session。

    规则(保守, 正确性优先):
    - 纯 SELECT(无 FOR UPDATE/SHARE) → read_engine
    - 其余(含不确定) → write_engine
    - 已开启事务(有写入脏对象/显式 begin)时: 第一次 bind 后会话绑定引擎,
      SQLAlchemy 会复用同一 bind, 避免读写混用两连接导致事务分裂。

    单库模式(read_engine is write_engine): 退化为普通 Session, 零行为变化。
    """

    def get_bind(self, mapper=None, *, clause=None, bind=None, **kw):
        # 显式 bind → 尊重
        if bind is not None:
            return bind
        # 事务已开启: 复用 SQLAlchemy 自己选的 bind, 避免同事务内读写分裂两连接
        if self.in_transaction():
            try:
                return super().get_bind(mapper, clause=clause, bind=bind, **kw)
            except Exception:  # noqa: BLE001
                return write_engine
        if (
            read_engine is not write_engine
            and clause is not None
            and is_read_clause(clause)
        ):
            return read_engine
        return write_engine


SessionLocal = sessionmaker(class_=RoutingSession, bind=write_engine)

# 专用工厂(需要强制读主库时用, 例如写后立即读 / 需要强一致)
WriteSessionLocal = sessionmaker(class_=Session, bind=write_engine)
ReadSessionLocal = sessionmaker(
    class_=Session,
    bind=read_engine,
    # 只读会话默认不自动 flush 脏对象(没有写路径)
)


def get_db(readonly: bool = False):
    """FastAPI 依赖注入。

    - 默认: RoutingSession(自动读写分流)
    - readonly=True: 强制只读引擎会话(调用方保证不写)
    """
    if readonly:
        db = ReadSessionLocal()
    else:
        db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def install_apm_on_engines() -> None:
    """给 write/read 引擎挂 APM 耗时追踪(P1, 幂等, 失败静默)。"""
    try:
        from src.core.apm import install_db_tracing

        install_db_tracing(write_engine)
        if read_engine is not write_engine:
            install_db_tracing(read_engine)
    except Exception:  # noqa: BLE001
        pass


install_apm_on_engines()
