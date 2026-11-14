"""多 worker 并发启动: init_db 的建表/迁移必须跨进程串行(2026-11-14 生产事故)。

事故: v0.11.17 首次部署时 WEB_WORKERS=4, 四个进程同时进 lifespan → init_db。
`Base.metadata.create_all(checkfirst=True)` 的 checkfirst 只在本进程内有效: 主进程
已建好 `premarket_factor_evidence`, 另一个 worker 的 checkfirst 还没看到就 CREATE,
报 `psycopg2.errors.DuplicateTable: relation "premarket_factor_evidence" already exists`,
父进程随之退出, 容器 Exited(0) → 健康检查始终不过 → 发版 fail-closed 停机。

只在 PostgreSQL 上可复现(生产口径), 所以本用例要求隔离 PG 管理连接:
    SIDA_TEST_PG_ADMIN=postgresql+psycopg2://<可建库角色>:<pw>@127.0.0.1:5432/postgres
未配置即 skip(不接受在既有库上跑)。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ADMIN_URL = (os.environ.get("SIDA_TEST_PG_ADMIN") or "").strip()
_DB_RE = re.compile(r"^sida_initdb_t_[0-9a-f]{10}$")
_ROLE_RE = re.compile(r"^sida_initdb_u_[0-9a-f]{10}$")
_FORBIDDEN = frozenset({"postgres", "template0", "template1", "sida", "sida-pro", "sida_pro"})
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ident(name: str, pattern: re.Pattern[str]) -> str:
    if pattern.fullmatch(name) is None or name in _FORBIDDEN:
        raise RuntimeError(f"拒绝操作非专属测试对象: {name!r}")
    return name


@pytest.fixture()
def pg_url():
    if not ADMIN_URL:
        pytest.skip("需要 SIDA_TEST_PG_ADMIN(可建库的隔离管理连接); 不接受在既有库上跑")
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - 连不上就跳过, 不伪装通过
        admin.dispose()
        pytest.skip(f"SIDA_TEST_PG_ADMIN 连不上: {type(exc).__name__}")

    suffix = uuid.uuid4().hex[:10]
    dbname = _ident(f"sida_initdb_t_{suffix}", _DB_RE)
    role = _ident(f"sida_initdb_u_{suffix}", _ROLE_RE)
    pwd = f"pw_{uuid.uuid4().hex[:16]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{pwd}\''))
        conn.execute(text(f'CREATE DATABASE "{dbname}" OWNER "{role}"'))
    url = make_url(ADMIN_URL).set(database=dbname, username=role, password=pwd)
    url = url.set(drivername="postgresql+psycopg2")
    url_str = url.render_as_string(hide_password=False)
    try:
        with create_engine(url_str, isolation_level="AUTOCOMMIT", future=True).connect() as conn:
            assert conn.execute(text("SELECT current_database()")).scalar_one() == dbname
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
    except Exception:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
            conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        admin.dispose()
        raise
    try:
        yield url_str
    finally:
        with admin.connect() as conn:
            conn.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :d AND pid <> pg_backend_pid()"
                ),
                {"d": dbname},
            )
            conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
            conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        admin.dispose()


def _init_db_env(url: str, data_dir: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": data_dir,
        "TMPDIR": data_dir,
        "DATA_DIR": data_dir,
        "PYTHONPATH": REPO_ROOT,
        "SIDA_DB_URL": url,
        "DATABASE_URL_WRITE": url,
    }
    # 不继承任何真实连接/凭据
    for key in ("DATABASE_URL_MIGRATE", "DATABASE_URL_READ", "SIDA_APP_DB_ROLE"):
        env.pop(key, None)
    return env


def test_concurrent_workers_can_start_on_empty_database(pg_url, tmp_path):
    """4 个进程同时 init_db(模拟 WEB_WORKERS=4)必须全部成功, 不得 DuplicateTable。"""
    env = _init_db_env(pg_url, str(tmp_path))
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", "from src.web.database import init_db; init_db()"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(4)
    ]
    out = [(p.wait(timeout=180), (p.stdout.read() if p.stdout else "")) for p in procs]
    for p in procs:
        if p.stdout:
            p.stdout.close()
    failed = [(rc, tail[-1500:]) for rc, tail in out if rc != 0]
    assert not failed, "并发 init_db 失败(应为跨进程串行):\n" + "\n---\n".join(t for _, t in failed)
    assert not any("DuplicateTable" in tail for _, tail in out), "出现建表竞态 DuplicateTable"

    engine = create_engine(pg_url, future=True)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT to_regclass('public.schema_migrations')")).scalar_one()
            assert conn.execute(
                text("SELECT to_regclass('public.premarket_factor_evidence')")
            ).scalar_one()
            assert conn.execute(
                text("SELECT count(*) FROM schema_migrations WHERE success = 0")
            ).scalar_one() == 0
    finally:
        engine.dispose()
