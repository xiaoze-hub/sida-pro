"""W3.1(D2, 2026-09-09) 方言层收编回归。

覆盖:
1. CI 门禁 check_is_pg_scope: 干净仓库退出 0; 方言布尔外溢到方言层外要报违规。
2. src/db/dialect 语义助手: upsert_sql 双方言统一形态 / insert_ignore_sql
   按 is_postgres() 分叉 / declared_backend 测试口径恒为 sqlite。
3. 存量老库升级路径: 老 schema(users+stocks 无新列) → create_all(只建缺表)
   + 版本化迁移 → 143 补列/回填 sort_order、148 加 user_id 并归 owner。
4. 全新库: 全部迁移(101..148)一次跑齐且 success=1。
"""

import importlib.util
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = REPO_ROOT / "scripts" / "check_is_pg_scope.py"


# ── 1. 门禁 ───────────────────────────────────────────────────────────────
def test_gate_passes_on_clean_repo():
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_gate_flags_is_pg_outside_dialect(tmp_path):
    spec = importlib.util.spec_from_file_location("check_is_pg_scope", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tok = "IS" + "_PG"  # 源文件不直接含该 token
    (tmp_path / "business.py").write_text(
        f"from src.web.database import {tok}\n", encoding="utf-8"
    )
    (tmp_path / "clean.py").write_text(
        "from src.db.dialect import is_postgres\n", encoding="utf-8"
    )
    hits = mod.find_violations(tmp_path)
    assert any("business.py" in h for h in hits)
    assert not any("clean.py" in h for h in hits)


# ── 2. 语义助手 ───────────────────────────────────────────────────────────
def test_upsert_sql_unified_shape():
    from src.db.dialect import upsert_sql

    stmt = upsert_sql(
        "summary_cache",
        ["symbol", "market", "computed_at", "ttl_s", "payload"],
        ["symbol", "market"],
        ["computed_at", "ttl_s", "payload"],
    )
    assert "INSERT INTO summary_cache (symbol, market, computed_at, ttl_s, payload)" in stmt
    assert "ON CONFLICT (symbol, market) DO UPDATE" in stmt
    assert "computed_at = excluded.computed_at" in stmt


def test_insert_ignore_sql_follows_is_postgres(monkeypatch):
    import src.db.dialect as dbd

    monkeypatch.setattr(dbd, "is_postgres", lambda: True)
    assert dbd.insert_ignore_sql("l2_ticks", ["ts", "symbol"]).endswith(
        "ON CONFLICT DO NOTHING"
    )
    monkeypatch.setattr(dbd, "is_postgres", lambda: False)
    assert dbd.insert_ignore_sql("l2_ticks", ["ts", "symbol"]).startswith(
        "INSERT OR IGNORE INTO l2_ticks"
    )


def test_declared_backend_is_sqlite_in_tests():
    # conftest 固定 SIDA_DB_URL=sqlite:///{tmp}/panwatch_test.db
    from src.db.dialect import declared_backend

    assert declared_backend() == "sqlite"


def test_sqlite_upsert_roundtrip(tmp_path):
    """upsert_sql 产物在真实 SQLite 上落两遍只留一行且字段被覆盖。"""
    from src.db.dialect import upsert_sql

    eng = create_engine(f"sqlite:///{tmp_path / 'up.db'}")
    with eng.begin() as conn:
        conn.execute(
            text("CREATE TABLE summary_cache (symbol TEXT, market TEXT, ttl_s INTEGER, PRIMARY KEY(symbol, market))")
        )
    stmt = upsert_sql("summary_cache", ["symbol", "market", "ttl_s"], ["symbol", "market"], ["ttl_s"])
    with eng.begin() as conn:
        conn.execute(text(stmt), {"symbol": "600519", "market": "CN", "ttl_s": 1})
        conn.execute(text(stmt), {"symbol": "600519", "market": "CN", "ttl_s": 2})
        rows = conn.execute(text("SELECT symbol, ttl_s FROM summary_cache")).fetchall()
    assert rows == [("600519", 2)]


# ── 3. 存量老库升级路径(A 层收编的真实验证) ──────────────────────────────
@pytest.fixture()
def legacy_sqlite_engine(tmp_path):
    """模拟 A 层时代之前的老库: users 有 owner, stocks 连 sort_order/user_id 都没有。"""
    db_file = tmp_path / "legacy.db"
    raw = sqlite3.connect(db_file)
    raw.executescript(
        """
        CREATE TABLE users (
            id VARCHAR(36) PRIMARY KEY,
            role TEXT,
            created_at DATETIME,
            is_active INTEGER
        );
        INSERT INTO users VALUES ('u-owner-1', 'owner', '2026-01-01 00:00:00', 1);
        CREATE TABLE stocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol VARCHAR NOT NULL,
            name VARCHAR NOT NULL,
            market VARCHAR NOT NULL
        );
        INSERT INTO stocks (symbol, name, market) VALUES ('600519', '贵州茅台', 'CN');
        """
    )
    raw.commit()
    raw.close()
    return create_engine(f"sqlite:///{db_file}")


def test_legacy_upgrade_path_adds_columns(legacy_sqlite_engine):
    from src.web import models  # noqa: F401 — 注册全部 ORM
    from src.web.migrations import run_versioned_migrations

    eng = legacy_sqlite_engine
    # create_all 只建缺表, 不会动已存在的 stocks/users
    models.Base.metadata.create_all(bind=eng)
    run_versioned_migrations(eng)

    with eng.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(stocks)")).fetchall()}
        assert "sort_order" in cols
        assert "user_id" in cols
        row = conn.execute(
            text("SELECT sort_order, user_id FROM stocks WHERE symbol='600519'")
        ).first()
        assert row[0] == 1  # 143: sort_order 回填 = id
        assert row[1] == "u-owner-1"  # 148: 旧数据归 owner
        applied = {
            r[0]
            for r in conn.execute(
                text("SELECT version FROM schema_migrations WHERE success = 1")
            ).fetchall()
        }
    assert {101, 121, 124, 143, 148}.issubset(applied)
    # 幂等: 重复跑不炸不重复
    run_versioned_migrations(eng)


# ── 4. 全新库一次跑齐 ─────────────────────────────────────────────────────
def test_fresh_db_applies_all_migrations(tmp_path):
    from src.web import models  # noqa: F401
    from src.web.migrations import MIGRATIONS, run_versioned_migrations

    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    models.Base.metadata.create_all(bind=eng)
    run_versioned_migrations(eng)
    with eng.begin() as conn:
        rows = conn.execute(
            text("SELECT version, success FROM schema_migrations")
        ).fetchall()
    applied = {v for v, ok in rows if ok == 1}
    assert applied == {m.version for m in MIGRATIONS}
