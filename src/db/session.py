"""ORM 会话入口(中立层, KI-039 切片 A, 2026-09-09; P1 读写分离 2026-09-18)。

从 `src/web/database.py` 下沉而来 —— 核心逻辑(`src/core/*`)不应依赖 Web 层:

- `Base`: ORM 元数据锚点(models 绑定于此)
- `engine` / `write_engine` / `read_engine` / `SessionLocal` / `get_db`
- 迁移/备份/建表等 **Web 侧初始化** 仍留在 `src/web/database.py::init_db()`

读写分离(P1 2026-09-18):
- `DATABASE_URL_WRITE` 主库(必填, 缺省回落 SIDA_DB_URL / SQLite)
- `DATABASE_URL_READ` 只读副本(可选); **未配置时 read_engine is write_engine**,
  与历史单库行为完全一致(向后兼容)。
- `SessionLocal` 使用 RoutingSession: SELECT 走读库, INSERT/UPDATE/DELETE 走写库。
- `engine` 保持指向写库 —— 历史 `engine.begin()` / `create_all(bind=engine)` 不受影响。

**写路径必须显式钉写库(2026-11-12 复审)**: RoutingSession 的第一条语句决定整条
事务绑在哪个引擎上。若某段代码先 SELECT(绑到读副本)再 INSERT/UPDATE, 写就落到
副本(只读 → 报错)或读写分裂成两条连接(读到旧值、提交不成原子)。所以凡是要写的
函数/端点一律:
1. 端点用 `Depends(get_write_db)`(专用写会话), 或
2. 核心函数在第一条语句之前调用 `pin_write_bind(db)`。

`pin_write_bind` 发现会话已经在读副本上开了事务时**直接报错**, 不静默分裂。

兼容: `from src.web.database import Base/engine/SessionLocal/get_db` 继续可用
(那边是 re-export); `src/db` 内部与 core 请改用本模块。
"""

from __future__ import annotations

import logging

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


logger = logging.getLogger(__name__)


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
    - 已被 `pin_write_bind` 钉住 → 一律走写库(整条事务都在主库);
    - 纯 SELECT(无 FOR UPDATE/SHARE) → read_engine
    - 其余(含不确定) → write_engine
    - 已开启事务时: 复用 SQLAlchemy 自己选的 bind, 不再换引擎(避免读写混用两连接)。

    单库模式(read_engine is write_engine): 退化为普通 Session, 零行为变化。

    注意: 「第一条语句是 SELECT」会把整条事务绑在读副本上。要写的会话必须
    先 `pin_write_bind`(或直接用 WriteSessionLocal), 否则后续写会落错引擎。
    """

    #: 是否已被 pin_write_bind 钉在写库(实例属性, 默认 False)。
    _pinned_write = False
    #: 最近一次选中的引擎: pin_write_bind 用它判断事务是否已经绑在副本上。
    _last_bind = None

    def get_bind(self, mapper=None, *, clause=None, bind=None, **kw):
        # 显式 bind → 尊重
        if bind is not None:
            return bind
        # 写路径已显式钉库: 读也走写库, 保证同一事务一个连接、读到自己的写
        if self._pinned_write:
            self._last_bind = write_engine
            return write_engine
        # 事务已开启: 复用 SQLAlchemy 自己选的 bind, 避免同事务内读写分裂两连接
        if self.in_transaction():
            try:
                chosen = super().get_bind(mapper, clause=clause, bind=bind, **kw)
            except Exception:  # noqa: BLE001
                chosen = write_engine
            self._last_bind = chosen
            return chosen
        if (
            read_engine is not write_engine
            and clause is not None
            and is_read_clause(clause)
        ):
            self._last_bind = read_engine
            return read_engine
        self._last_bind = write_engine
        return write_engine


SessionLocal = sessionmaker(class_=RoutingSession, bind=write_engine)

# 专用工厂(需要强制读主库时用, 例如写后立即读 / 需要强一致)
WriteSessionLocal = sessionmaker(class_=Session, bind=write_engine)
ReadSessionLocal = sessionmaker(
    class_=Session,
    bind=read_engine,
    # 只读会话默认不自动 flush 脏对象(没有写路径)
)


class ReadOnlyBindError(RuntimeError):
    """写操作被要求跑在只读副本(或已绑副本的事务)上。

    这是**编程错误**, 不吞不降级: 静默继续就会写进副本(直接报只读错)或把
    事务拆成「副本读 + 主库写」两条连接(读到旧值、提交非原子)。
    """


def pin_write_bind(db):
    """把会话显式钉在写库; 必须在**第一条语句之前**调用。

    RoutingSession 的事务引擎在第一条语句处定型。因子权重这类
    「先读旧值再写新值」的路径若直接复用路由会话, SELECT 会把事务绑到读副本:
    随后写副本报错, 或读到副本的滞后旧值算出错误的 before/after。

    返回同一个会话(方便链式使用):
    - None → None(调用方自己决定);
    - 非 RoutingSession(专用写会话 / 测试会话)→ 原样返回;
    - RoutingSession 未开事务 → 打上 `_pinned_write` 标记(后续读写都走写库);
    - RoutingSession 的事务已经开在读副本上 → 抛 ReadOnlyBindError(不静默分裂);
    - RoutingSession 的事务已经开在写库上 → 只打标记(后续读不跳到副本)。
    """
    if db is None:
        return db
    if not isinstance(db, RoutingSession):
        return db
    if getattr(db, "_pinned_write", False):
        return db
    if db.in_transaction() and getattr(db, "_last_bind", None) is not write_engine:
        raise ReadOnlyBindError(
            "写操作不能复用在只读副本上已开启的事务; "
            "请用 src.db.session.WriteSessionLocal() 或提前 pin_write_bind"
        )
    db._pinned_write = True
    db.bind = write_engine
    return db


def get_write_db():
    """FastAPI 依赖: **专用写会话**(不路由到读副本)。

    写端点(权重覆盖、标定指纹等)用它, 以免「先 SELECT 后写」把事务绑到副本。
    """
    db = WriteSessionLocal()
    try:
        yield db
    finally:
        db.close()


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
