"""pytest 全局 conftest — 确保 import data_source 路径"""
import sys
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent
# 让 `import data_source` 能找到 /home/ubuntu/sida-src/data_source
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest


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
