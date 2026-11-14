"""隔离 PostgreSQL 上验证: 旧数据全局汇总 / system 桶独立对账(2026-11-13)。

为什么必须真 PG: 生产是 PostgreSQL。`user_id IS NULL AND owner_scope IS NULL` 的三值逻辑、
部分唯一索引(M250)对"旧行不受约束、system 桶受约束"的差别, 在 SQLite 上不能等价证明。

安全边界(与既有 PG 用例同一套):
  1. 唯一入口 `SIDA_TEST_PG_ADMIN`(可 CREATEDB/CREATEROLE 的隔离管理连接); 没配就 skip;
  2. 每次运行新建 `sida_ref_t_<hex>` 库 + `sida_ref_u_<hex>` 角色, 名字过正则, 拒绝既有库名;
  3. 建完核对 current_database()/current_user;
  4. 结束 terminate → DROP DATABASE → DROP ROLE。

运行:
    SIDA_TEST_PG_ADMIN=postgresql+psycopg2://<可建库角色>:<pw>@127.0.0.1:5432/postgres \
        python -m pytest -q tests/test_learning_reference_pg.py
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

ADMIN_URL = (
    os.environ.get("SIDA_TEST_PG_ADMIN") or os.environ.get("QUOTA_TEST_PG_ADMIN") or ""
).strip()

_DB_RE = re.compile(r"^sida_ref_t_[0-9a-f]{10}$")
_ROLE_RE = re.compile(r"^sida_ref_u_[0-9a-f]{10}$")
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
    dbname = _ident(f"sida_ref_t_{suffix}", _DB_RE)
    role = _ident(f"sida_ref_u_{suffix}", _ROLE_RE)
    pwd = f"pw_{uuid.uuid4().hex[:16]}"
    base = make_url(ADMIN_URL)
    admin_user = str(base.username or "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", admin_user):
        admin.dispose()
        pytest.skip(f"管理连接用户名不合规, 不做 GRANT: {admin_user!r}")

    def _drop_objects() -> None:
        with admin.connect() as conn:
            # PG16 的 DROP DATABASE ... WITH (FORCE) 会踢掉残留连接; 以库属主身份执行,
            # 不需要 SUPERUSER(pg_terminate_backend 在非超级用户下可能无权)。
            try:
                conn.execute(text(f'SET ROLE "{role}"'))
                conn.execute(text(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)'))
            except Exception as exc:  # noqa: BLE001 - 清理失败不掩盖测试结论
                print(f"[pg cleanup] drop database failed: {type(exc).__name__}")
            finally:
                try:
                    conn.execute(text("RESET ROLE"))
                except Exception:  # noqa: BLE001
                    pass
            conn.execute(text(f'DROP ROLE IF EXISTS "{role}"'))

    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{pwd}\''))
            conn.execute(text(f'GRANT "{role}" TO "{admin_user}"'))
            conn.execute(text(f'CREATE DATABASE "{dbname}" OWNER "{role}"'))
    except Exception:
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
            assert inspect(conn).get_table_names() == [], "新库必须是空的"
        yield eng
    finally:
        eng.dispose()
        _drop_objects()
        admin.dispose()


def _session(pg_env):
    from src.web import models as M  # noqa: F401
    from src.web.database import Base

    Base.metadata.create_all(pg_env)
    return sessionmaker(bind=pg_env)()


def _today_minus(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def _seed(s, *, uid, agent, action, ret, day, symbol, owner_scope, status="evaluated"):
    from src.web.models import AgentPredictionOutcome

    s.add(AgentPredictionOutcome(
        user_id=uid, owner_scope=owner_scope, agent_name=agent, stock_symbol=symbol,
        stock_market="CN", prediction_date=day, horizon_days=1, action=action,
        action_label=action, outcome_return_pct=ret, outcome_status=status,
    ))


def _seed_user(s, uid: str) -> None:
    """真 PG 有 user_id 外键: 个人行必须先有 users 行。"""
    from src.web.models import User

    s.add(User(
        id=uid, username=f"user-{uid}", password_hash="x", role="member",
        is_active=True, token_version=1,
    ))
    # 先落 users 再落后验行: 两者无 ORM 关系, 不 flush 时插入顺序不保证, 会撞外键。
    s.flush()


def test_m250_partial_indexes_cover_system_but_not_legacy_rows(pg_env):
    """旧行(双 NULL)不受部分唯一索引约束; system 桶受约束 —— 这是聚合去重的前提。"""
    s = _session(pg_env)
    day = _today_minus(9)
    # 同一键的两条历史行: 旧表没有部分唯一索引, 必须允许
    _seed(s, uid=None, agent="a", action="buy", ret=1.0, day=day,
          symbol="600519", owner_scope=None)
    _seed(s, uid=None, agent="a", action="buy", ret=-1.0, day=day,
          symbol="600519", owner_scope=None)
    s.commit()

    # system 桶同一键第二条非 superseded 行: 必须被部分唯一索引拒绝
    _seed(s, uid=None, agent="a", action="buy", ret=1.0, day=day,
          symbol="300750", owner_scope="system")
    s.commit()
    _seed(s, uid=None, agent="a", action="buy", ret=2.0, day=day,
          symbol="300750", owner_scope="system")
    with pytest.raises(IntegrityError):
        s.commit()
    s.rollback()
    s.close()


def test_legacy_and_system_aggregates_are_isolated_on_real_pg(pg_env):
    """两个用户 + 旧记录 + system 同时存在: 三路聚合互不混算, 旧记录按最早行去重。"""
    from src.core.learning_reference import (
        legacy_reference_summary,
        system_reference_summary,
    )

    s = _session(pg_env)
    day = _today_minus(12)
    _seed_user(s, "u-a")
    _seed_user(s, "u-b")
    _seed(s, uid="u-a", agent="a", action="buy", ret=5.0, day=day,
          symbol="600519", owner_scope="user")
    _seed(s, uid="u-b", agent="a", action="buy", ret=-5.0, day=day,
          symbol="000001", owner_scope="user")
    # 旧记录: 2 条同键重复 + 1 条不同键; buy 涨 = 命中
    _seed(s, uid=None, agent="legacy-agent", action="buy", ret=3.0, day=day,
          symbol="688981", owner_scope=None)
    _seed(s, uid=None, agent="legacy-agent", action="buy", ret=-3.0, day=day,
          symbol="688981", owner_scope=None)
    _seed(s, uid=None, agent="legacy-agent", action="sell", ret=2.0, day=day,
          symbol="688982", owner_scope=None)
    # system: 独立
    _seed(s, uid=None, agent="sys-agent", action="buy", ret=-2.0, day=day,
          symbol="300750", owner_scope="system")
    s.commit()

    legacy = legacy_reference_summary(s, min_n=1)
    assert legacy["scope"] == "legacy_unowned"
    assert legacy["global_only"] is True and legacy["attributable_to_user"] is False
    assert legacy["sample"]["evaluated_rows"] == 3, "旧记录只应有 3 条(2 用户 + system 不算)"
    assert legacy["sample"]["directional_sample"] == 3
    assert legacy["duplicate_risk"]["key_groups_with_duplicates"] == 1
    assert legacy["duplicate_risk"]["rows_in_duplicate_groups"] == 2
    assert legacy["overall"]["raw_total"] == 3
    assert legacy["overall"]["total"] == 2, "同键按最早一行去重"
    # 最早那行是 +3.0(buy 命中); 另一条 sell +2.0 未命中
    assert legacy["overall"]["hit_count"] == 1
    assert [a["agent"] for a in legacy["agents"]] == ["legacy-agent"]

    system = system_reference_summary(s, min_n=1)
    assert system["scope"] == "system" and system["independent_of_personal"] is True
    assert system["sample"]["evaluated_rows"] == 1
    assert system["overall"]["total"] == 1 and system["overall"]["hit_count"] == 0
    assert [a["agent"] for a in system["agents"]] == ["sys-agent"]

    # 个人行一行未改: 两个账号各自的记录仍在
    from src.web.models import AgentPredictionOutcome
    assert s.query(AgentPredictionOutcome).filter(
        AgentPredictionOutcome.owner_scope == "user").count() == 2
    s.close()


def test_system_reconcile_preview_is_zero_write_on_real_pg(pg_env):
    """只读对账预览在真 PG 上同样零写入, 且个人 pending 行不在查询范围。"""
    from src.core.learning_reference import system_reconcile_preview

    s = _session(pg_env)
    day = _today_minus(20)
    _seed_user(s, "u-a")
    _seed(s, uid=None, agent="sys", action="buy", ret=None, day=day,
          symbol="300750", owner_scope="system", status="pending")
    _seed(s, uid="u-a", agent="a", action="buy", ret=None, day=day,
          symbol="600519", owner_scope="user", status="pending")
    s.commit()

    def _fetch(symbol, market):
        from src.core.trading_calendar import add_trading_days

        target = add_trading_days(date.fromisoformat(day), 1)
        return [SimpleNamespace(date=day, close=10.0),
                SimpleNamespace(date=target.isoformat(), close=11.0)]

    before = s.execute(text(
        "SELECT count(*), count(outcome_price) FROM agent_prediction_outcomes"
    )).one()
    result = system_reconcile_preview(s, kline_fetcher=_fetch)
    assert result["dry_run"] is True and result["read_only"] is True
    assert result["writes"] == 0 and result["executed"] is False
    assert result["total_pending"] == 1
    assert result["would_evaluate"] == 1
    s.commit()
    after = s.execute(text(
        "SELECT count(*), count(outcome_price) FROM agent_prediction_outcomes"
    )).one()
    assert tuple(after) == tuple(before), "只读预览改变了数据行"
    assert s.execute(text(
        "SELECT count(*) FROM agent_prediction_outcomes WHERE outcome_status <> 'pending'"
    )).scalar() == 0
    s.close()


def test_system_persistent_reconcile_is_isolated_and_idempotent_on_real_pg(pg_env):
    """真 PG: system pending -> 持久 evaluated; 个人/历史一行不动; 重跑幂等。"""
    from src.core.learning_reference import (
        SYSTEM_RECONCILE_VERSION,
        reconcile_system_outcomes,
    )
    from src.web.models import AgentPredictionOutcome

    s = _session(pg_env)
    pred = _today_minus(20)
    target = date.fromisoformat(pred)
    from src.core.trading_calendar import add_trading_days

    target = add_trading_days(target, 1)
    _seed_user(s, "u-a")
    _seed(s, uid=None, agent="sys", action="buy", ret=None, day=pred,
          symbol="300750", owner_scope="system", status="pending")
    _seed(s, uid="u-a", agent="a", action="buy", ret=None, day=pred,
          symbol="600519", owner_scope="user", status="pending")
    _seed(s, uid=None, agent="legacy", action="buy", ret=None, day=pred,
          symbol="688981", owner_scope=None, status="pending")
    s.commit()

    def _fetch(symbol, market):
        return [SimpleNamespace(date=pred, close=10.0),
                SimpleNamespace(date=target.isoformat(), close=11.0)]

    result = reconcile_system_outcomes(s, kline_fetcher=_fetch)
    assert result["executed"] is True and result["db_committed"] is True
    assert result["writes"] == 1 and result["evaluated"] == 1
    assert result["total_pending"] == 1 and result["batch_size"] == 1
    assert result["orders_placed"] == 0 and result["weights_changed"] is False

    sys_row = s.query(AgentPredictionOutcome).filter(
        AgentPredictionOutcome.owner_scope == "system").one()
    assert sys_row.outcome_status == "evaluated"
    assert round(sys_row.outcome_return_pct, 4) == 10.0
    audit = (sys_row.meta or {}).get("reconcile") or {}
    assert audit.get("version") == SYSTEM_RECONCILE_VERSION
    assert audit.get("data_date") == target.isoformat()
    assert audit.get("collected_at") and audit.get("dedupe_rule")

    # 个人行 / 历史行: 仍是 pending, 没有被 system 对账写入
    for row in s.query(AgentPredictionOutcome).filter(
        AgentPredictionOutcome.owner_scope.in_([None, "user"])
    ).all():
        assert row.outcome_status == "pending"
        assert row.outcome_price is None

    evaluated_at = sys_row.evaluated_at
    result2 = reconcile_system_outcomes(s, kline_fetcher=_fetch)
    assert result2["evaluated"] == 0 and result2["writes"] == 0
    s.refresh(sys_row)
    assert sys_row.outcome_status == "evaluated"
    assert sys_row.evaluated_at == evaluated_at
    s.close()
