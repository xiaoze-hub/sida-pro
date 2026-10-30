"""验证容器数据库默认策略，使用子进程隔离模块级 engine 与 session。"""
import os
import subprocess
import sys


def _check(env_overrides: dict[str, str], code: str):
    env = os.environ.copy()
    for key in ("DOCKER", "SIDA_DB_URL", "SIDA_ALLOW_SQLITE", "DATABASE_URL_READ", "DATABASE_URL_WRITE", "DATABASE_URL"):
        env.pop(key, None)
    env.update(env_overrides)
    return subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)


def test_docker_without_db_url_fails_fast():
    result = _check({"DOCKER": "1"}, "import src.web.database")
    assert result.returncode != 0
    assert "SIDA_DB_URL" in result.stderr


def test_docker_with_pg_url_ok():
    code = "from src.db.dialect import is_postgres; from src.web.database import DB_URL; assert is_postgres(); assert DB_URL.startswith('postgresql')"
    result = _check({"DOCKER": "1", "SIDA_DB_URL": "postgresql+psycopg2://sida:x@pg:5432/sida"}, code)
    assert result.returncode == 0, result.stderr


def test_local_default_still_sqlite():
    code = "from src.db.dialect import is_postgres; from src.web.database import DB_URL; assert not is_postgres(); assert DB_URL.startswith('sqlite')"
    result = _check({}, code)
    assert result.returncode == 0, result.stderr
