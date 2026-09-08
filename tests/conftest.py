"""pytest 全局 conftest — 确保 import data_source 路径"""
import os
import sys
import tempfile
from pathlib import Path

# 0.4② (2026-09-08) 方言门禁: 测试即本地开发, 显式声明 SQLite, 免得任何测试
# 在启动门禁生效后因缺 SIDA_DB_URL 被拒。个别测试用 monkeypatch.delenv 覆盖。
os.environ.setdefault("SIDA_ALLOW_SQLITE", "1")

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
