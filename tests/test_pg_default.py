"""PG 默认口径回归: 容器内无 SIDA_DB_URL 必须 fail-fast, 本地默认仍是 SQLite。

2026-09-08: 历史教训 — env 丢失时生产曾静默落在容器内 sqlite
(database is locked + 重建丢数据)。以后 DOCKER=1 无连接串直接崩启动。

2026-09-09: conftest 给全部测试 setdefault 了 SIDA_ALLOW_SQLITE=1(0.4② 的
逃生口), 会把 fail-fast 门整个豁免 — _reload_database 必须把它一起摘掉再
reload, 结束后原样恢复(否则 CI 门禁永远红)。
"""

import importlib
import os

import pytest

_RELOAD_KEYS = ("DOCKER", "SIDA_DB_URL", "SIDA_ALLOW_SQLITE")


@pytest.fixture(autouse=True)
def _restore_database_module():
    """reload 是对同一模块对象的原地操作 —— 用例结束后必须按恢复后的 env
    再 reload 一次, 否则本文件留下的 DB_URL/engine 会污染后续所有用
    SessionLocal 的测试(实测曾把迁移跑进真实 data/panwatch.db)。"""
    yield
    import src.web.database as db

    importlib.reload(db)


def _reload_database(env: dict):
    saved = {k: os.environ.get(k) for k in _RELOAD_KEYS}
    for k in _RELOAD_KEYS:
        os.environ.pop(k, None)
    os.environ.update(env)
    import src.web.database as db

    try:
        return importlib.reload(db)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_docker_without_db_url_fails_fast():
    try:
        _reload_database({"DOCKER": "1"})
    except RuntimeError as e:
        assert "SIDA_DB_URL" in str(e)
    else:
        raise AssertionError("DOCKER=1 无 SIDA_DB_URL 应该 RuntimeError, 实际静默启动了")


def test_docker_with_pg_url_ok():
    db = _reload_database(
        {"DOCKER": "1", "SIDA_DB_URL": "postgresql+psycopg2://sida:x@pg:5432/sida"}
    )
    assert db.IS_PG is True


def test_local_default_still_sqlite():
    db = _reload_database({})
    assert db.IS_PG is False
    assert db.DB_URL.startswith("sqlite")
