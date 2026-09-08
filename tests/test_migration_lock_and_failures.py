"""W1.5(A5) 迁移加固测试(2026-09-08):

1. PG 多实例并发启动 → run_versioned_migrations 先拿会话级 advisory lock,
   持锁横跨全部迁移, finally 解锁+关连接(inner 抛异常也不例外)。
2. 迁移失败 → schema_migrations 落 success=0 + error(新连接写入),
   修复后重跑同 version → success=1。
3. checksum 变更 → 迁移重跑(旧记录不挡新代码)。
4. 新收编迁移 _m138/_m139 建表存在(跑在 sqlite 上验证 DDL 语法)。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from src.web import migrations as mig
from src.web.migrations import Migration, has_pending_migrations, run_versioned_migrations


class _FakeDialect:
    def __init__(self, name: str):
        self.name = name


class _FakeLockConn:
    def __init__(self) -> None:
        self.sqls: list[str] = []
        self.closed = False
        self.fail_on: str | None = None

    def execution_options(self, **kw):
        return self

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if self.fail_on and self.fail_on in sql:
            raise ConnectionError(f"模拟连接故障: {sql}")
        self.sqls.append(sql)
        return None

    def close(self) -> None:
        self.closed = True


class _FakePgEngine:
    """只测锁包装层: dialect.name=postgresql; inner 被 monkeypatch, 不触真库。"""

    def __init__(self) -> None:
        self.dialect = _FakeDialect("postgresql")
        self.lock_conn = _FakeLockConn()
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        return self.lock_conn


class _FakeSqliteEngine:
    """dialect=sqlite 的假引擎: 断言 sqlite 路径完全不碰 connect()(不加锁)。"""

    def __init__(self) -> None:
        self.dialect = _FakeDialect("sqlite")
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        raise AssertionError("sqlite 路径不应为 advisory lock 建连接")


def test_pg_advisory_lock_wraps_inner(monkeypatch):
    eng = _FakePgEngine()
    calls: list[str] = []
    monkeypatch.setattr(mig, "_run_migrations_inner", lambda e: calls.append("inner"))

    run_versioned_migrations(eng)

    assert eng.connect_calls == 1
    assert calls == ["inner"]
    sqls = eng.lock_conn.sqls
    assert sqls and "pg_advisory_lock" in sqls[0]
    assert any("pg_advisory_unlock" in s for s in sqls)
    assert eng.lock_conn.closed is True
    # 顺序: lock 最先, unlock 在 inner 之后
    assert "pg_advisory_unlock" in sqls[-1]


def test_pg_advisory_unlock_even_when_inner_fails(monkeypatch):
    eng = _FakePgEngine()

    def _boom(e):
        raise RuntimeError("migration exploded")

    monkeypatch.setattr(mig, "_run_migrations_inner", _boom)

    with pytest.raises(RuntimeError, match="migration exploded"):
        run_versioned_migrations(eng)

    assert eng.lock_conn.closed is True
    assert any("pg_advisory_unlock" in s for s in eng.lock_conn.sqls)


def test_pg_lock_acquire_failure_closes_conn_and_skips_inner(monkeypatch):
    eng = _FakePgEngine()
    eng.lock_conn.fail_on = "pg_advisory_lock"
    inner_called: list[str] = []
    monkeypatch.setattr(
        mig, "_run_migrations_inner", lambda e: inner_called.append("inner")
    )

    with pytest.raises(ConnectionError):
        run_versioned_migrations(eng)

    assert inner_called == []
    assert eng.lock_conn.closed is True


def test_sqlite_path_never_takes_advisory_lock(monkeypatch):
    eng = _FakeSqliteEngine()
    called: list[str] = []
    monkeypatch.setattr(mig, "_run_migrations_inner", lambda e: called.append("inner"))

    run_versioned_migrations(eng)

    assert called == ["inner"]
    assert eng.connect_calls == 0


@pytest.fixture()
def sqlite_engine():
    return create_engine("sqlite://")


def test_failure_records_error_then_rerun_recovers(sqlite_engine, monkeypatch):
    def bad(conn):
        raise RuntimeError("boom-ddl")

    def good(conn):
        conn.execute(text("CREATE TABLE IF NOT EXISTS w15_demo (id INTEGER PRIMARY KEY)"))

    monkeypatch.setattr(mig, "MIGRATIONS", (Migration(9901, "w15_bad", bad),))
    with pytest.raises(RuntimeError, match="boom-ddl"):
        run_versioned_migrations(sqlite_engine)

    with sqlite_engine.begin() as conn:
        row = conn.execute(
            text("SELECT success, error FROM schema_migrations WHERE version = 9901")
        ).first()
    assert row is not None
    assert row[0] == 0
    assert "boom-ddl" in str(row[1])
    assert has_pending_migrations(sqlite_engine) is True

    # 修复后重跑: 同 version 换 runner(新 checksum) → success=1
    monkeypatch.setattr(mig, "MIGRATIONS", (Migration(9901, "w15_bad", good),))
    run_versioned_migrations(sqlite_engine)
    with sqlite_engine.begin() as conn:
        ok = conn.execute(
            text("SELECT success FROM schema_migrations WHERE version = 9901")
        ).scalar()
        cnt = conn.execute(text("SELECT COUNT(*) FROM w15_demo")).scalar()
    assert ok == 1
    assert cnt == 0
    assert has_pending_migrations(sqlite_engine) is False


def test_checksum_mismatch_reapplies_migration(sqlite_engine, monkeypatch):
    def v1(conn):
        conn.execute(text("CREATE TABLE w15_chk (x INTEGER)"))

    monkeypatch.setattr(mig, "MIGRATIONS", (Migration(9902, "w15_chk", v1),))
    run_versioned_migrations(sqlite_engine)

    # 模拟代码演进: 库里记录的 checksum 与新代码不一致 → 必须重跑
    with sqlite_engine.begin() as conn:
        conn.execute(
            text("UPDATE schema_migrations SET checksum = 'deadbeef' WHERE version = 9902")
        )
    calls: list[int] = []

    def v2(conn):
        calls.append(1)
        conn.execute(text("ALTER TABLE w15_chk ADD COLUMN y INTEGER"))

    monkeypatch.setattr(mig, "MIGRATIONS", (Migration(9902, "w15_chk", v2),))
    run_versioned_migrations(sqlite_engine)

    assert calls == [1]
    with sqlite_engine.begin() as conn:
        ok = conn.execute(
            text("SELECT success FROM schema_migrations WHERE version = 9902")
        ).scalar()
    assert ok == 1


def test_m138_m139_create_tables_on_sqlite(sqlite_engine, monkeypatch):
    monkeypatch.setattr(
        mig,
        "MIGRATIONS",
        (
            Migration(138, "market_flow_snapshots_table", mig._m138_market_flow_snapshots_table),
            Migration(139, "mainline_rank_daily_table", mig._m139_mainline_rank_daily_table),
        ),
    )
    run_versioned_migrations(sqlite_engine)

    with sqlite_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO market_flow_snapshots (total_main_flow, up_count, down_count,"
                " flat_count, sh_flow, sz_flow) VALUES (1.5, 10, 20, 3, 0.8, 0.7)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO mainline_rank_daily (date, name, rank, score)"
                " VALUES ('2026-09-08', 'AI算力', 1, 99.5)"
            )
        )
        snap = conn.execute(text("SELECT COUNT(*) FROM market_flow_snapshots")).scalar()
        rank = conn.execute(
            text("SELECT rank, score FROM mainline_rank_daily WHERE name = 'AI算力'")
        ).first()
    assert snap == 1
    assert rank is not None and rank[0] == 1


def test_m140_141_142_orm_model_tables_on_sqlite(sqlite_engine, monkeypatch):
    """收编 market_scan.py / signal_summary.py 兜底建表的三个 ORM 迁移。"""
    monkeypatch.setattr(
        mig,
        "MIGRATIONS",
        (
            Migration(140, "market_scan_ranks_table", mig._m140_market_scan_ranks_table),
            Migration(141, "signal_summary_daily_table", mig._m141_signal_summary_daily_table),
            Migration(142, "dark_fund_top_snapshots_table", mig._m142_dark_fund_top_snapshots_table),
        ),
    )
    run_versioned_migrations(sqlite_engine)

    with sqlite_engine.begin() as conn:
        names = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
    assert {"market_scan_ranks", "signal_summary_daily", "dark_fund_top_snapshots"} <= names
