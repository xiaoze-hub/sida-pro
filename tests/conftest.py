"""pytest 全局 conftest — 确保 import data_source 路径"""
import os
import sys
import tempfile
from pathlib import Path

# 0.4② (2026-09-08) 方言门禁: 测试即本地开发, 显式声明 SQLite, 免得任何测试
# 在启动门禁生效后因缺 SIDA_DB_URL 被拒。个别测试用 monkeypatch.delenv 覆盖。
os.environ.setdefault("SIDA_ALLOW_SQLITE", "1")

# 2026-09-20: skill key 的盐必须**固定** —— 与生产同一前提。
# 生产事故: SKILL_KEY_SALT 未设 ⇒ 每进程随机盐 ⇒ 同一把 key 时而 200 时而 401。
# 代码已改成"盐不固定就拒绝签发(503)", 所以测试环境**必须**给一把固定盐,
# 否则凡是要签 key 的用例都会被正确拒签而变红(本次实测: 3 个用例红)。
# 必须在 src.web.api.skills_gateway **import 之前**设置 —— 该模块在 import 期读 env。
os.environ.setdefault("SKILL_KEY_SALT", "test-fixed-salt-2f9c1d4e7a3b5086")

# W2.2/E4 (2026-09-09) 测试隔离: 测试绝不触碰真实 DATA_DIR 与真实库文件。
# 实测仓库 data/panwatch.db 被测试跑迁移留下 7 个 .bak(2026-09-08~09)。
# 必须在任何 src.* 导入之前设置 —— error_tracker(_FILE)/disk_cache(_CACHE_DIR)/
# media_utils(MEDIA_DIR) 都在模块 import 时把 DATA_DIR 烤进模块级常量,
# fixture 里再改 env 为时已晚。
_ORIG_DATA_DIR = os.environ.get("DATA_DIR")  # 留给 _verify_real_data_untouched 快照
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="sida_test_data_"))
os.environ["DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["PANWATCH_DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["PANWATCH_CACHE_DIR"] = str(_TEST_DATA_DIR / "cache")
# zhitu vendor 的本地库回退路径(marketdata/vendors/zhitu.py _DB_PATH)也一并隔离
os.environ.setdefault("PANWATCH_DB", str(_TEST_DATA_DIR / "panwatch.db"))
# 默认 SQLite 库一并隔离: database.py 的 DB_PATH 跟随 DATA_DIR(见 database.py
# W2.2/E4 注), 再配 SIDA_DB_URL setdefault 双保险。显式传 SIDA_DB_URL 的工作流
# (test_error_tracker 头注释)不受影响(setdefault)。
os.environ.setdefault(
    "SIDA_DB_URL", f"sqlite:///{(_TEST_DATA_DIR / 'panwatch_test.db')}"
)

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
# 让 `import data_source` 能找到 /home/ubuntu/sida-src/data_source
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest


def _snapshot_dir(d: Path) -> dict:
    """目录快照: 相对路径 → (mtime_ns, size)。目录不存在视为空。"""
    if not d.is_dir():
        return {}
    out = {}
    for p in d.rglob("*"):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(d))] = (st.st_mtime_ns, st.st_size)
    return out


@pytest.fixture(scope="session", autouse=True)
def _verify_real_data_untouched():
    """W2.2/E4 验收: 会话结束后真实 DATA_DIR 内文件 mtime/大小无变化。

    "真实 DATA_DIR" 取调用方显式设置的 DATA_DIR; 未设置时取仓库 data/
    (本地各模块 ./data 回退默认值的实际落点)。会话中被改 → fail。
    """
    real_dir = Path(_ORIG_DATA_DIR) if _ORIG_DATA_DIR else PROJECT_ROOT / "data"
    before = _snapshot_dir(real_dir)
    yield
    after = _snapshot_dir(real_dir)
    if before != after:
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])
        raise AssertionError(
            f"测试会话改动了真实数据目录 {real_dir}: "
            f"新增={added[:5]} 删除={removed[:5]} 变更={changed[:5]}"
        )


@pytest.fixture(scope="session", autouse=True)
def _init_test_db():
    """CI 门禁修复(2026-08-21): 测试直接用 SessionLocal() 查表
    (如 test_announcement_eval 查 stocks), 但 CI 环境没人调 init_db(),
    SQLite 文件库无表 → OperationalError: no such table。

    session 级 autouse fixture: 任何测试首次运行前 create_all 建全表。
    本地已有库时 create_all 幂等, 无副作用。
    """
    from src.web.database import init_db

    init_db()


def _is_sqlite(db) -> bool:
    try:
        return db.get_bind().dialect.name == "sqlite"
    except Exception:  # noqa: BLE001
        return False


def purge_users(db, *, ids=None, only_username=None, exclude_username=None) -> int:
    """删除用户时**先删引用它们的子表行**(否则撞 FK), 返回删掉的子表行数。

    2026-09-18: 多用户之后 `users.id` 被 `user_sessions` / `skill_api_keys` /
    `pro_applications` / `high_value_api_logs` 等表 FK 引用, 于是
    `DELETE FROM users WHERE username != 'admin'` 直接
    "FOREIGN KEY constraint failed" —— 夹具在 teardown 炸掉, 库留脏数据, 后续用例连坐
    (全量跑时表现为 22 errors + 3 个登录态用例红, 单跑却绿)。

    做法: 按 `Base.metadata.sorted_tables` 的依赖拓扑序(父在前) **反序**遍历所有表,
    把"FK 指向 users"的列里命中目标 id 的行先删干净, 再删 users 本身。
    以后新增任何引用 users 的表都自动被覆盖, 不用回来改这个函数。
    """
    import time

    from sqlalchemy import text

    from src.web.database import Base
    from src.web.models import User

    if ids is not None:
        target_ids = [str(i) for i in ids]
    else:
        q = db.query(User.id)
        if only_username is not None:
            q = q.filter(User.username == only_username)
        if exclude_username is not None:
            q = q.filter(User.username != exclude_username)
        target_ids = [str(row[0]) for row in q.all()]
    ids = target_ids
    if not ids:
        return 0

    # SQLite 并发: 本函数要连删十几张表, 而库里可能还有别的连接在跑(被测接口自己的 session /
    # 后台线程)。先调大 busy_timeout 让它**等锁**而不是立刻 `database is locked`
    # (2026-09-18: 全量跑时 14 个 teardown ERROR 就是这个锁)。
    if _is_sqlite(db):
        try:
            db.execute(text("PRAGMA busy_timeout=15000"))
        except Exception:  # noqa: BLE001 - 方言差异, 失败就按原行为走
            pass

    users_table = User.__table__
    removed = 0
    for table in reversed(Base.metadata.sorted_tables):
        if table is users_table:
            continue
        for col in table.columns:
            for fk in col.foreign_keys:
                try:
                    target = fk.column.table
                except Exception:  # noqa: BLE001 - 解析不到的 FK 跳过(不猜)
                    continue
                if target is users_table:
                    for attempt in range(3):
                        try:
                            res = db.execute(table.delete().where(col.in_(ids)))
                            removed += res.rowcount or 0
                            break
                        except Exception as e:  # noqa: BLE001
                            if "locked" in str(e).lower() and attempt < 2:
                                time.sleep(0.5 * (attempt + 1))  # 等锁(别的连接在写)
                                continue
                            raise
                    break
    db.query(User).filter(User.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return removed


@pytest.fixture(autouse=True)
def _clear_module_caches():
    """缓存类测试隔离(2026-08-21): kline_collector / marketdata 的模块级
    TTL 缓存跨测试残留 → 单跑过、合跑挂(flaky)。每个测试前统一清空。
    """
    try:
        from src.collectors import kline_collector

        kline_collector.clear_kline_cache()
    except Exception:
        pass
    try:
        from src.collectors import capital_flow_collector

        capital_flow_collector._FLOW_CACHE.clear()
    except Exception:
        pass
    yield
