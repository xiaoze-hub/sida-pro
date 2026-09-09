"""ORM 会话入口(中立层, KI-039 切片 A, 2026-09-09)。

从 `src/web/database.py` 下沉而来 —— 核心逻辑(`src/core/*`)不应依赖 Web 层:

- `Base`: ORM 元数据锚点(models 绑定于此)
- `engine` / `SessionLocal` / `get_db`: 引擎与会话
- 迁移/备份/建表等 **Web 侧初始化** 仍留在 `src/web/database.py::init_db()`

兼容: `from src.web.database import Base/engine/SessionLocal/get_db` 继续可用
(那边是 re-export); `src/db` 内部与 core 请改用本模块。
"""

from sqlalchemy.orm import DeclarativeBase, sessionmaker

from src.db.dialect import DB_PATH, DB_URL, acquire_write, build_engine  # noqa: F401


# reload 防御(沿用 src/web/database 原注释): importlib.reload 保留 module
# __dict__, 必须复用旧 Base, 否则已绑定旧 Base 的 ORM 模型会与新建的
# Base.metadata 脱钩(create_all 建出空库)。
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
