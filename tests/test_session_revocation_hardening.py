"""会话墓碑表缺失的事故回归(2026-10-23 生产 500)。

事故链, 必须逐环钉死:
  1. `auth_session_revocations` 只在「踢设备」写入路径懒建 → 生产从未踢过人就**没表**;
  2. `is_session_revoked()` 在**每个鉴权请求**都 SELECT 它 → PostgreSQL 抛
     `UndefinedTable` 并**中止整个事务**;
  3. 兜底 `except` 只 return False 不回滚 → 该请求后续所有查询
     `InFailedSqlTransaction` → `/api/settings`、`/api/notifications`、
     `/api/users/admin/list` 成片 500, 用户看到的是「数据都没了」。

为什么现有用例全绿: SQLite 里一条语句失败**不会**中止事务, 所以第 3 环在测试环境
根本复现不出来。因此这里不测「接口返回 200」, 而是直接钉两条不变量:
  (a) 建表必须走启动期迁移(不依赖偶然的写路径);
  (b) 任何 fail-open 的 DB 探测失败后**必须回滚**, 否则事务留给下一个查询。
"""

from __future__ import annotations

import pytest


def test_migration_219_registered_and_idempotent():
    """建表交给迁移, 且可重复执行(生产会跑两次以上也不会炸)。"""
    from sqlalchemy import create_engine, text

    from src.web.migrations import MIGRATIONS, _m219_auth_session_revocations

    versions = {m.version: m for m in MIGRATIONS}
    assert 219 in versions, "墓碑表必须由启动期迁移创建, 不能只在写路径懒建"
    assert versions[219].name == "auth_session_revocations"

    eng = create_engine("sqlite://")
    with eng.begin() as conn:
        _m219_auth_session_revocations(conn)
        _m219_auth_session_revocations(conn)  # 幂等
        row = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": "auth_session_revocations"},
        ).first()
        assert row is not None
        cols = {
            r[1]
            for r in conn.execute(text("PRAGMA table_info(auth_session_revocations)"))
        }
    assert cols == {"session_id", "expires_at", "revoked_at"}


@pytest.fixture()
def _spy_db():
    """真 SQLite 会话 + 记录 rollback 是否被调用。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    eng = create_engine("sqlite://")
    db = sessionmaker(bind=eng)()

    calls: list[int] = []
    real_rollback = db.rollback

    def _rollback():
        calls.append(1)
        return real_rollback()

    db.rollback = _rollback  # type: ignore[method-assign]
    try:
        yield db, calls
    finally:
        db.close()


def test_missing_revocation_table_is_survivable(_spy_db):
    """表不存在时: 放行(不 401) + 回滚(不把事务留给下一个查询)。"""
    from sqlalchemy import text

    from src.core.permissions import is_session_revoked, session_is_active

    db, calls = _spy_db

    assert is_session_revoked(db, "jti-abc") is False
    assert calls, "探测失败后未回滚 → PG 事务保持 aborted, 后续查询全 500"

    assert session_is_active(db, "jti-abc", "uid-1") is True
    # 回滚之后同一个会话必须还能跑查询(这就是生产里被打成 500 的那一步)
    assert db.execute(text("SELECT 1")).first()[0] == 1


def test_probe_failure_does_not_poison_session(_spy_db, monkeypatch):
    """探测函数整体抛错时也不许污染会话(fail-open + rollback)。"""
    from sqlalchemy import text

    import src.core.permissions as perm

    db, calls = _spy_db

    def _boom(*a, **k):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(perm, "is_session_revoked", _boom)
    assert perm.session_is_active(db, "jti-abc", "uid-1") is True
    assert calls
    assert db.execute(text("SELECT 1")).first()[0] == 1


def test_record_session_failure_rolls_back(_spy_db, monkeypatch):
    """登录写会话失败也不许污染(否则登录接口自己就 500)。"""
    from sqlalchemy import text

    import src.core.permissions as perm
    from src.db.models import User

    db, calls = _spy_db

    class _User:
        id = "uid-1"
        username = "u"

    def _boom(*a, **k):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(db, "query", _boom)
    perm.record_session(db, _User(), "jti-x", __import__("datetime").datetime.now())
    assert calls
    assert db.execute(text("SELECT 1")).first()[0] == 1

    # User 只是用来确认导入没被这次改动破坏
    assert User.__tablename__ == "users"


def test_mark_session_revoked_failure_rolls_back(_spy_db, monkeypatch):
    """踢设备写墓碑失败也不许污染(踢人接口不能因此 500)。"""
    from datetime import datetime, timezone

    from sqlalchemy import text

    import src.core.permissions as perm

    db, calls = _spy_db
    monkeypatch.setattr(db, "execute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("ddl")))
    perm.mark_session_revoked(db, "jti-y", datetime.now(timezone.utc))
    assert calls
