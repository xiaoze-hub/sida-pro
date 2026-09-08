"""W2.2/E4 (2026-09-09): 测试进程不得触碰真实 DATA_DIR 与真实库文件。

背景: conftest 的 autouse init_db 曾把全部迁移跑进仓库 data/panwatch.db
(实测 2026-09-08~09 期间留下 7 个迁移备份 .bak), error_tracker/disk_cache/
media_utils 等在模块 import 时就把 DATA_DIR 烤进模块级常量 —— 隔离必须在
任何 src.* 导入前生效(conftest 顶层), 不能靠 fixture。
"""
import os
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


def test_data_dir_isolated_to_tmp():
    d = Path(os.environ["DATA_DIR"])
    assert d.is_relative_to(Path(tempfile.gettempdir())), f"DATA_DIR 未隔离: {d}"
    assert not d.is_relative_to(PROJECT_ROOT), f"DATA_DIR 指向仓库内: {d}"
    assert Path(os.environ["PANWATCH_DATA_DIR"]) == d


def test_default_db_url_isolated():
    """显式传 SIDA_DB_URL 的工作流(test_error_tracker 头注释)不受 conftest
    影响; 未显式传时, conftest 必须把默认 SQLite 指进临时目录, 而不是仓库
    data/panwatch.db(database.py 的 DB_PATH 与 DATA_DIR 无关)。"""
    from src.web import database

    if os.environ.get("SIDA_DB_URL_ETL_EXTERNAL") == "1":  # pragma: no cover
        pytest.skip("外部显式 URL 工作流")
    if database.DB_URL.startswith("sqlite"):
        assert "sida_test_data" in database.DB_URL, (
            f"默认 SQLite 落在真实路径: {database.DB_URL}"
        )


def test_error_tracker_file_isolated():
    """error_tracker 在 import 时烤死 _FILE —— 若真实 DATA_DIR 未被隔离,
    模块级常量会指向 /app/data(或仓库 data/)。"""
    import src.core.error_tracker as et

    if os.environ.get("ERROR_TRACKER_FILE"):  # pragma: no cover
        pytest.skip("外部显式 ERROR_TRACKER_FILE")
    assert Path(et._FILE).is_relative_to(Path(tempfile.gettempdir())), (
        f"error_tracker 落在真实 DATA_DIR: {et._FILE}"
    )


def test_delenv_sida_db_url_reload_stays_isolated(tmp_path):
    """结构性回归(E4 核心): delenv SIDA_DB_URL 后(模拟 test_pg_default 的
    reload 路径), 默认库必须落在 DATA_DIR 内(依托 database.py DB_PATH 跟随
    DATA_DIR), 而不是仓库 data/panwatch.db。

    子进程验证: 不在进程内 reload database 模块 —— reload 会重执行模块级
    代码, 对其它测试是全局状态污染源; 子进程同时覆盖"全新进程首启"的真实
    场景。"""
    import subprocess
    import sys

    data_dir = tmp_path / "sida_test_data_sub"
    data_dir.mkdir()
    code = (
        "import os, sys;"
        f"os.environ['DATA_DIR'] = r'{data_dir}';"
        "os.environ.pop('SIDA_DB_URL', None);"
        f"sys.path.insert(0, r'{PROJECT_ROOT}');"
        "from src.web.database import DB_URL;"
        "assert DB_URL.startswith('sqlite'), DB_URL;"
        "assert 'sida_test_data_sub' in DB_URL, DB_URL;"
        "print('ISOLATED_OK')"
    )
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 0, f"子进程断言失败: {r.stdout}\n{r.stderr}"
    assert "ISOLATED_OK" in r.stdout
