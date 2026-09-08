"""PG 默认口径回归: 容器内无 SIDA_DB_URL 必须 fail-fast, 本地默认仍是 SQLite。

2026-09-08: 历史教训 — env 丢失时生产曾静默落在容器内 sqlite
(database is locked + 重建丢数据)。以后 DOCKER=1 无连接串直接崩启动。
"""

import importlib
import os


def _reload_database(env: dict):
    for k in ("DOCKER", "SIDA_DB_URL"):
        os.environ.pop(k, None)
    os.environ.update(env)
    import src.web.database as db

    return importlib.reload(db)


def test_docker_without_db_url_fails_fast():
    try:
        _reload_database({"DOCKER": "1"})
    except RuntimeError as e:
        assert "SIDA_DB_URL" in str(e)
    else:
        raise AssertionError("DOCKER=1 无 SIDA_DB_URL 应该 RuntimeError, 实际静默启动了")
    finally:
        _reload_database({})


def test_docker_with_pg_url_ok():
    db = _reload_database(
        {"DOCKER": "1", "SIDA_DB_URL": "postgresql+psycopg2://sida:x@pg:5432/sida"}
    )
    try:
        assert db.IS_PG is True
    finally:
        _reload_database({})


def test_local_default_still_sqlite():
    db = _reload_database({})
    try:
        assert db.IS_PG is False
        assert db.DB_URL.startswith("sqlite")
    finally:
        _reload_database({})
