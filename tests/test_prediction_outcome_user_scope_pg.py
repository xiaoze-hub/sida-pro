"""真实 PostgreSQL 上验证 m249 的用户隔离 / 部分唯一键（2026-11-12 P0）。

为什么必须真 PG: 生产是 PostgreSQL, 而"部分唯一索引 + 并发插入"的语义在 SQLite 上
无法等价证明(写事务是库级互斥)。但**绝不允许**在任何既有库上跑:

  1. 唯一入口 `SIDA_TEST_PG_ADMIN`(可 CREATEDB/CREATEROLE 的隔离管理连接); 没配就 skip,
     宁可显示"未验证", 也不拿某个现成库冒充;
  2. 每次运行新建 `sida_predoutcome_t_<hex>` 库 + `sida_predoutcome_u_<hex>` 角色, 名字过正则,
     且拒绝 postgres/template*/sida 等既有库名;
  3. 建完先核对 current_database()/current_user 就是刚建的那两个;
  4. finally 里 terminate → DROP DATABASE → DROP ROLE。

运行:
    SIDA_TEST_PG_ADMIN=postgresql+psycopg2://<可建库角色>:<pw>@127.0.0.1:5432/postgres \
        python -m pytest -q tests/test_prediction_outcome_user_scope_pg.py
"""
from __future__ import annotations

import os
import re
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

ADMIN_URL = (
    os.environ.get("SIDA_TEST_PG_ADMIN") or os.environ.get("QUOTA_TEST_PG_ADMIN") or ""
).strip()

_DB_RE = re.compile(r"^sida_predoutcome_t_[0-9a-f]{10}$")
_ROLE_RE = re.compile(r"^sida_predoutcome_u_[0-9a-f]{10}$")
_FORBIDDEN = frozenset({"postgres", "template0", "template1", "sida", "sida-pro", "sida_pro"})


def _ident(name: str, pattern: re.Pattern[str]) -> str:
    if pattern.fullmatch(name) is None or name in _FORBIDDEN:
        raise RuntimeError(f"拒绝操作非专属测试对象: {name!r}")
    return name


@pytest.fixture()
def pg_env():
    if not ADMIN_URL:
        pytest.skip("需要 SIDA_TEST_PG_ADMIN(可建库的隔离管理连接); 不接受在既有库上跑")
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - 连不上就跳过, 不伪装通过
        admin.dispose()
        pytest.skip(f"SIDA_TEST_PG_ADMIN 连不上: {type(exc).__name__}: {exc}")

    suffix = uuid.uuid4().hex[:10]
    dbname = _ident(f"sida_predoutcome_t_{suffix}", _DB_RE)
    role = _ident(f"sida_predoutcome_u_{suffix}", _ROLE_RE)
    pwd = f"pw_{uuid.uuid4().hex[:16]}"
    base = make_url(ADMIN_URL)
    admin_user = str(base.username or "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", admin_user):
        admin.dispose()
        pytest.skip(f"管理连接用户名不合规, 不做 GRANT: {admin_user!r}")

    def _drop_objects() -> None:
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

    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{pwd}\''))
            # PG16: 建"属于该角色"的库要求当前用户能 SET ROLE 到它
            conn.execute(text(f'GRANT "{role}" TO "{admin_user}"'))
            conn.execute(text(f'CREATE DATABASE "{dbname}" OWNER "{role}"'))
    except Exception:
        # 建库建角色中途失败也必须拆干净(否则残留一次性角色/库)
        try:
            _drop_objects()
        finally:
            admin.dispose()
        raise

    test_url = base.set(database=dbname, username=role, password=pwd)
    eng = create_engine(test_url, future=True)
    try:
        with eng.connect() as conn:
            assert conn.execute(text("SELECT current_database()")).scalar() == dbname
            assert conn.execute(text("SELECT current_user")).scalar() == role
            tables = inspect(conn).get_table_names()
            assert tables == [], f"新库必须是空的, 实际有表: {tables}"
        yield eng
    finally:
        eng.dispose()
        _drop_objects()
        admin.dispose()


LEGACY_DDL = """
CREATE TABLE users (
    id VARCHAR(36) PRIMARY KEY,
    username VARCHAR(64) NOT NULL
);
CREATE TABLE agent_prediction_outcomes (
    id SERIAL PRIMARY KEY,
    agent_name VARCHAR(64) NOT NULL,
    stock_symbol VARCHAR(32) NOT NULL,
    stock_market VARCHAR(16) NOT NULL DEFAULT 'CN',
    prediction_date VARCHAR(10) NOT NULL,
    horizon_days INTEGER NOT NULL DEFAULT 1,
    action VARCHAR(16) NOT NULL DEFAULT 'watch',
    action_label VARCHAR(32) NOT NULL DEFAULT '观望',
    confidence DOUBLE PRECISION,
    trigger_price DOUBLE PRECISION,
    outcome_price DOUBLE PRECISION,
    outcome_return_pct DOUBLE PRECISION,
    outcome_status VARCHAR(16) NOT NULL DEFAULT 'pending',
    meta JSON,
    evaluated_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


def _seed_users(conn, *ids: str) -> None:
    for uid in ids:
        conn.execute(
            text("INSERT INTO users(id, username) VALUES (:i, :n)"),
            {"i": uid, "n": f"user-{uid}"},
        )


def test_m249_on_real_pg_keeps_legacy_rows_and_enforces_ownership(pg_env):
    """m249: 加列 + 用户归属外键(存量不改写), 老的无状态唯一键**不再创建**。

    2026-11-12 三次修正: 老版本 m249 建 `ux_prediction_outcome_user_key`, 一旦库里已有
    "计分行 + superseded 版本行", 重跑(checksum 变化)建索引就会失败 → 启动迁移中断。
    DB 层唯一性改由 m250 的部分唯一索引承担, 所以这里断言它**不存在**。
    """
    from src.web import migrations as mig

    with pg_env.begin() as conn:
        conn.execute(text(LEGACY_DDL))
        _seed_users(conn, "u-a", "u-b")
        # 加列之前的老库: 同一键已有两行(历史重复), 归属不可证明
        conn.execute(
            text(
                "INSERT INTO agent_prediction_outcomes "
                "(agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action) "
                "VALUES ('premarket_outlook','600183','CN','2026-11-11',1,'buy'),"
                "       ('premarket_outlook','600183','CN','2026-11-11',1,'sell')"
            )
        )

    with pg_env.begin() as conn:
        mig._m249_prediction_outcome_user_scope(conn)

    with pg_env.begin() as conn:
        cols = {c["name"]: c for c in inspect(conn).get_columns("agent_prediction_outcomes")}
        assert "user_id" in cols and cols["user_id"]["nullable"] is True
        indexes = {i["name"] for i in inspect(conn).get_indexes("agent_prediction_outcomes")}
        assert "ix_prediction_outcome_user" in indexes
        assert "ux_prediction_outcome_user_key" not in indexes, (
            "老的无状态唯一键会挡住版本追加、并让重跑建索引失败, 新版 m249 不再创建"
        )

        # 归属外键: 与 create_all 新库同名同语义, 且已校验(无孤儿行)
        fk = conn.execute(
            text(
                "SELECT c.conname, c.convalidated, pg_get_constraintdef(c.oid) "
                "FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'agent_prediction_outcomes' AND c.contype = 'f'"
            )
        ).fetchall()
        assert len(fk) == 1, f"应有且仅有一个归属外键, 实际 {fk}"
        name, validated, definition = fk[0]
        assert name == "agent_prediction_outcomes_user_id_fkey"
        assert validated is True, "无孤儿行时必须 VALIDATE, 否则约束只对新行生效"
        assert "REFERENCES users(id)" in definition
        assert "ON DELETE SET NULL" in definition

        # 存量行一个都没动
        rows = conn.execute(
            text("SELECT user_id, action FROM agent_prediction_outcomes ORDER BY id")
        ).fetchall()
        assert len(rows) == 2
        assert all(r[0] is None for r in rows)
        assert [r[1] for r in rows] == ["buy", "sell"]

        # 幂等: 再跑一次不炸、不改数据
        mig._m249_prediction_outcome_user_scope(conn)
        assert conn.execute(text("SELECT COUNT(*) FROM agent_prediction_outcomes")).scalar() == 2

    # 外键生效: 不存在的用户写不进去(ghost 行不再可能产生)
    with pytest.raises(IntegrityError):
        with pg_env.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_prediction_outcomes "
                    "(user_id, agent_name, stock_symbol, stock_market, prediction_date, "
                    "horizon_days, action) "
                    "VALUES ('ghost-uid','premarket_outlook','600183','CN','2026-11-11',1,'buy')"
                )
            )
    # 存在的用户可以写; 删除用户 → ON DELETE SET NULL(行保留, 归属置空)
    with pg_env.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_prediction_outcomes "
                "(user_id, agent_name, stock_symbol, stock_market, prediction_date, "
                "horizon_days, action) "
                "VALUES ('u-a','premarket_outlook','600185','CN','2026-11-11',1,'buy')"
            )
        )
    with pg_env.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = 'u-a'"))
    with pg_env.begin() as conn:
        assert conn.execute(
            text("SELECT user_id FROM agent_prediction_outcomes WHERE stock_symbol = '600185'")
        ).fetchall() == [(None,)], "删除用户必须按 SET NULL 保留行, 而不是连带删掉预测"
        assert conn.execute(text("SELECT COUNT(*) FROM agent_prediction_outcomes")).scalar() == 3


def test_m249_second_user_and_concurrent_insert_on_real_pg(pg_env):
    """另一个账号同键可各存一行; 两个连接真并发插入只成功一个。

    DB 层唯一性来自 m250 的计分行部分唯一索引(新版 m249 不再建无状态唯一键),
    所以这里跑完整的 249+250 链路。
    """
    from src.web import migrations as mig

    with pg_env.begin() as conn:
        conn.execute(text(LEGACY_DDL))
        _seed_users(conn, "u-a", "u-b", "u-c")
    with pg_env.begin() as conn:
        mig._m249_prediction_outcome_user_scope(conn)
        mig._m250_prediction_outcome_versions_and_system_scope(conn)

    insert_sql = text(
        "INSERT INTO agent_prediction_outcomes "
        "(user_id, agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action) "
        "VALUES (:u,'premarket_outlook','600183','CN','2026-11-11',1,:a)"
    )
    with pg_env.begin() as conn:
        conn.execute(insert_sql, {"u": "u-a", "a": "buy"})
        conn.execute(insert_sql, {"u": "u-b", "a": "sell"})
        assert conn.execute(
            text("SELECT COUNT(*) FROM agent_prediction_outcomes WHERE user_id IS NOT NULL")
        ).scalar() == 2

    # 真并发: 两个独立连接同时插同键同用户 —— 一个成功, 另一个撞唯一键
    results: list[str] = []
    import threading

    barrier = threading.Barrier(2)

    def worker(tag: str):
        with pg_env.connect() as conn:
            barrier.wait()
            try:
                conn.execute(insert_sql, {"u": "u-c", "a": tag})
                conn.commit()
                results.append("ok")
            except IntegrityError:
                conn.rollback()
                results.append("dup")

    threads = [threading.Thread(target=worker, args=(t,)) for t in ("buy", "sell")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["dup", "ok"], results
    with pg_env.begin() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM agent_prediction_outcomes WHERE user_id = 'u-c'")
        ).scalar() == 1


def test_m250_on_real_pg_allows_version_append_and_system_bucket(pg_env):
    """真实 PG: m249 的无状态唯一键被 m250 替换; 版本可追加、system 桶独立、存量行不动。

    每个"期望失败"的插入都单独开一个事务块 —— PG 事务一旦因唯一键失败就整体 aborted,
    不能在同一条 connection 上继续执行(那会变成 InFailedSqlTransaction, 掩盖真正的断言)。
    """
    from src.web import migrations as mig

    with pg_env.begin() as conn:
        conn.execute(text(LEGACY_DDL))
        _seed_users(conn, "u-a")
        conn.execute(
            text(
                "INSERT INTO agent_prediction_outcomes "
                "(agent_name, stock_symbol, stock_market, prediction_date, horizon_days, action) "
                "VALUES ('premarket_outlook','600183','CN','2026-11-11',1,'buy'),"
                "       ('premarket_outlook','600183','CN','2026-11-11',1,'sell')"
            )
        )
    with pg_env.begin() as conn:
        mig._m249_prediction_outcome_user_scope(conn)
        mig._m250_prediction_outcome_versions_and_system_scope(conn)

    with pg_env.begin() as conn:
        cols = {c["name"] for c in inspect(conn).get_columns("agent_prediction_outcomes")}
        assert {"user_id", "run_trace_id", "owner_scope"} <= cols
        names = {i["name"] for i in inspect(conn).get_indexes("agent_prediction_outcomes")}
        assert "ux_prediction_outcome_user_key" not in names, "m250 必须去掉 m249 的无状态唯一键"
        assert {
            "ux_prediction_outcome_user_active_key",
            "ux_prediction_outcome_user_run_key",
            "ux_prediction_outcome_system_active_key",
            "ux_prediction_outcome_system_run_key",
        } <= names

        # 存量行原样: user_id/owner_scope/run_trace_id 全空, 动作不变
        rows = conn.execute(
            text(
                "SELECT user_id, owner_scope, run_trace_id, action "
                "FROM agent_prediction_outcomes ORDER BY id"
            )
        ).fetchall()
        assert len(rows) == 2
        assert all(r[0] is None and r[1] is None and r[2] is None for r in rows)
        assert [r[3] for r in rows] == ["buy", "sell"]

    user_insert = text(
        "INSERT INTO agent_prediction_outcomes "
        "(user_id, owner_scope, run_trace_id, agent_name, stock_symbol, stock_market, "
        "prediction_date, horizon_days, action, outcome_status) "
        "VALUES (:u, :scope, :trace, 'premarket_outlook','600183','CN','2026-11-11',1,:action,"
        ":status)"
    )
    # 计分行 + 追加的 superseded 版本可以并存
    with pg_env.begin() as conn:
        conn.execute(user_insert, {"u": "u-a", "scope": "user", "trace": "r1", "action": "buy", "status": "pending"})
        conn.execute(user_insert, {"u": "u-a", "scope": "user", "trace": "r2", "action": "sell", "status": "superseded"})
    # 第二条计分行被拒
    with pytest.raises(IntegrityError):
        with pg_env.begin() as conn:
            conn.execute(user_insert, {"u": "u-a", "scope": "user", "trace": "r3", "action": "hold", "status": "pending"})
    # 同一 run 重复落库被 run 唯一键挡住
    with pytest.raises(IntegrityError):
        with pg_env.begin() as conn:
            conn.execute(user_insert, {"u": "u-a", "scope": "user", "trace": "r1", "action": "hold", "status": "superseded"})
    # system 桶: user_id 空, 与个人行互不冲突; 计分行同样唯一
    with pg_env.begin() as conn:
        conn.execute(user_insert, {"u": None, "scope": "system", "trace": "s1", "action": "buy", "status": "pending"})
    with pytest.raises(IntegrityError):
        with pg_env.begin() as conn:
            conn.execute(user_insert, {"u": None, "scope": "system", "trace": "s2", "action": "sell", "status": "pending"})
    with pg_env.begin() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM agent_prediction_outcomes WHERE user_id = 'u-a'")
        ).scalar() == 2
        assert conn.execute(
            text(
                "SELECT COUNT(*) FROM agent_prediction_outcomes "
                "WHERE owner_scope = 'system' AND user_id IS NULL"
            )
        ).scalar() == 1

    # system 桶不得绑定真实用户: DB 层 CHECK 直接拒(不只是应用层拒写)
    with pytest.raises(IntegrityError):
        with pg_env.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_prediction_outcomes "
                    "(user_id, owner_scope, run_trace_id, agent_name, stock_symbol, stock_market, "
                    "prediction_date, horizon_days, action, outcome_status) "
                    "VALUES ('u-a','system','s9','premarket_outlook','600186','CN','2026-11-11',"
                    "1,'buy','pending')"
                )
            )


def test_m249_m250_rerun_is_safe_with_version_rows_on_real_pg(pg_env):
    """重跑安全(真 PG): 库里已有"计分行 + superseded 版本行"时, 重跑 249/250 不能抛错。

    这是本轮要修的真实故障形态: 老版 m249 的 `CREATE UNIQUE INDEX ... WHERE user_id IS NOT
    NULL` 在有版本行的库上重跑(函数体改动 → checksum 变化 → 框架重跑)直接 UNIQUE 冲突,
    整个启动迁移中断。修法 = m249 不再建那条索引 + m250 的唯一索引"无冲突才建"。
    """
    from src.web import migrations as mig

    with pg_env.begin() as conn:
        conn.execute(text(LEGACY_DDL))
        _seed_users(conn, "u-a")
    with pg_env.begin() as conn:
        mig._m249_prediction_outcome_user_scope(conn)
        mig._m250_prediction_outcome_versions_and_system_scope(conn)

    insert_sql = text(
        "INSERT INTO agent_prediction_outcomes "
        "(user_id, owner_scope, run_trace_id, agent_name, stock_symbol, stock_market, "
        "prediction_date, horizon_days, action, outcome_status) "
        "VALUES ('u-a', 'user', :t, 'premarket_outlook','600183','CN','2026-11-11',1,'buy',:s)"
    )
    with pg_env.begin() as conn:
        conn.execute(insert_sql, {"t": "r1", "s": "pending"})
        conn.execute(insert_sql, {"t": "r2", "s": "superseded"})
    with pg_env.begin() as conn:
        before = conn.execute(
            text(
                "SELECT id, user_id, owner_scope, run_trace_id, outcome_status, action "
                "FROM agent_prediction_outcomes ORDER BY id"
            )
        ).fetchall()

    # 模拟"函数体改了 → checksum 变了 → 框架重跑", 顺序与实际一致(m249 先, m250 后)
    with pg_env.begin() as conn:
        mig._m249_prediction_outcome_user_scope(conn)
    with pg_env.begin() as conn:
        mig._m250_prediction_outcome_versions_and_system_scope(conn)
    with pg_env.begin() as conn:
        after = conn.execute(
            text(
                "SELECT id, user_id, owner_scope, run_trace_id, outcome_status, action "
                "FROM agent_prediction_outcomes ORDER BY id"
            )
        ).fetchall()
        names = {i["name"] for i in inspect(conn).get_indexes("agent_prediction_outcomes")}
    assert after == before, f"重跑改动了存量行: {before} -> {after}"
    assert "ux_prediction_outcome_user_key" not in names
    assert {
        "ux_prediction_outcome_user_active_key",
        "ux_prediction_outcome_user_run_key",
        "ux_prediction_outcome_system_active_key",
        "ux_prediction_outcome_system_run_key",
    } <= names


def test_m249_keeps_preexisting_ghost_rows_and_adds_not_valid_fk_on_real_pg(pg_env):
    """老库已有 ghost 行(user_id 指向已不存在用户)时:

    - 迁移**不删除、不改写**这些行(也不能让上线失败);
    - 外键以 `NOT VALID` 加上: 新写入立刻受约束, 存量孤儿行保持原样(不 VALIDATE);
    - 这是"不改存量数据"与"新写不再产生 ghost"的唯一同时成立的方案。
    """
    from src.web import migrations as mig

    with pg_env.begin() as conn:
        conn.execute(text(LEGACY_DDL))
        # 老库已有 user_id 列(手工/早期版本), 且其中一行指向不存在的用户
        conn.execute(text("ALTER TABLE agent_prediction_outcomes ADD COLUMN user_id VARCHAR(36)"))
        _seed_users(conn, "u-a")
        conn.execute(
            text(
                "INSERT INTO agent_prediction_outcomes "
                "(user_id, agent_name, stock_symbol, stock_market, prediction_date, "
                "horizon_days, action) VALUES "
                "('ghost-uid','premarket_outlook','600183','CN','2026-11-11',1,'buy'),"
                "('u-a','premarket_outlook','600184','CN','2026-11-11',1,'sell')"
            )
        )

    with pg_env.begin() as conn:
        mig._m249_prediction_outcome_user_scope(conn)

    with pg_env.begin() as conn:
        fk = conn.execute(
            text(
                "SELECT c.conname, c.convalidated FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid "
                "WHERE t.relname = 'agent_prediction_outcomes' AND c.contype = 'f'"
            )
        ).fetchall()
        assert len(fk) == 1, fk
        assert fk[0][0] == "agent_prediction_outcomes_user_id_fkey"
        assert fk[0][1] is False, "有存量孤儿行时不能 VALIDATE(那会因历史数据让上线失败)"
        rows = conn.execute(
            text("SELECT user_id FROM agent_prediction_outcomes ORDER BY id")
        ).fetchall()
        assert [r[0] for r in rows] == ["ghost-uid", "u-a"], "存量 ghost 行被改写了"

    # 新写入仍然受约束
    with pytest.raises(IntegrityError):
        with pg_env.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_prediction_outcomes "
                    "(user_id, agent_name, stock_symbol, stock_market, prediction_date, "
                    "horizon_days, action) "
                    "VALUES ('ghost-2','premarket_outlook','600185','CN','2026-11-11',1,'buy')"
                )
            )
