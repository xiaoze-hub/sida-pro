"""用户级配额的「先数后写」竞态 —— 必须在**真 PostgreSQL**上验证（2026-11-07 复审）。

为什么不能拿 SQLite 充数：SQLite 写事务是库级互斥，`COUNT(*)` 与 `INSERT` 之间
本来就不会有第二个写者插进来，所以任何竞态在 SQLite 上都"自然通过"。生产是
PostgreSQL，没有行锁就没有互斥 —— 只在 SQLite 上跑绿的用例证明不了任何并发安全性。

本文件的规矩：

1. **隔离资源**：`QUOTA_TEST_PG_ADMIN` 给出一个有 CREATEDB/CREATEROLE 权限的管理
   连接；用例自己建**唯一命名**的角色 + 数据库，`finally` 里先断连接再 DROP
   DATABASE / DROP ROLE。绝不碰任何既有库（尤其是生产库 sida-pro）。
2. **真并发**：两个独立 session（两条连接）在 `threading.Barrier` 上对齐后同时
   冲同一用户的上限边界；断言「只有一个成功、另一个 403、总数不超额」。
3. **两条写路径都要测**：HTTP `create_alert_rule` 与 AI `apply_action(kind=alert)`，
   外加一条混合并发（API vs Agent）。

没有 `QUOTA_TEST_PG_ADMIN` 时整体 skip —— 宁可显示"没验证"，也不许把 SQLite
的通过伪装成 PG 的通过。
"""

from __future__ import annotations

import os
import threading
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from src.core import free_tier
from src.db.models import Base, CopilotAction, PriceAlertRule, Stock, User
from src.harness import actions as act
from src.web.api.price_alerts import (
    AlertConditionGroup,
    AlertConditionItem,
    PriceAlertCreate,
    create_alert_rule,
)

ADMIN_URL = os.environ.get("QUOTA_TEST_PG_ADMIN")
pytestmark = pytest.mark.skipif(
    not ADMIN_URL,
    reason="需要 QUOTA_TEST_PG_ADMIN（可建库建角色的隔离 PG 管理连接）；SQLite 不能替代",
)

LIMIT = 3  # MEMBER_ALERT_MAX 默认值


def _validate_admin_url(url: str) -> None:
    parsed = make_url(url)
    if (parsed.get_backend_name() != "postgresql"
            or parsed.host not in {"127.0.0.1", "::1", "localhost"}
            or parsed.database != "postgres"):
        raise ValueError("并发破坏性测试只允许本机 postgres 维护库")


def _admin_engine():
    assert ADMIN_URL
    _validate_admin_url(ADMIN_URL)
    return create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", future=True)


@pytest.fixture(scope="module")
def pg():
    """建一套一次性角色/数据库，用完 finally 拆干净。"""
    suffix = uuid.uuid4().hex[:10]
    role = f"qt_{suffix}"
    pwd = f"pw_{suffix}"
    dbname = f"qt_{suffix}"
    admin = _admin_engine()
    engine = None
    role_created = False
    db_created = False
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE ROLE "{role}" LOGIN PASSWORD \'{pwd}\''))
            role_created = True
            c.execute(text(f'CREATE DATABASE "{dbname}" OWNER "{role}"'))
            db_created = True
        url = make_url(ADMIN_URL).set(username=role, password=pwd, database=dbname)
        engine = create_engine(url, future=True, pool_size=5, max_overflow=5)
        with engine.connect() as conn:
            actual = conn.execute(text("SELECT current_database(), current_user")).one()
            if tuple(actual) != (dbname, role):
                raise RuntimeError("临时数据库身份不匹配")
        Base.metadata.create_all(engine)
        yield engine, dbname, role
    finally:
        if engine is not None:
            engine.dispose()
        with admin.connect() as c:
            if db_created:
                c.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :d AND pid <> pg_backend_pid()"
                    ),
                    {"d": dbname},
                )
                c.execute(text(f'DROP DATABASE IF EXISTS "{dbname}"'))
            if role_created:
                c.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
        admin.dispose()


@pytest.fixture()
def factory(pg):
    engine, _, _ = pg
    free_tier.invalidate_cache()
    maker = sessionmaker(bind=engine, future=True)
    s = maker()
    try:
        yield maker, s
    finally:
        s.rollback()
        s.close()
        free_tier.invalidate_cache()


def _user(db, uid: str, role: str = "member") -> User:
    row = db.query(User).filter(User.id == uid).first()
    if row is None:
        row = User(id=uid, username=f"u-{uid}", password_hash="x", role=role)
        db.add(row)
        db.commit()
    return row


def _stock(db, uid: str, symbol: str) -> Stock:
    row = Stock(user_id=uid, symbol=symbol, name=f"股{symbol}", market="CN")
    db.add(row)
    db.commit()
    return row


def _rule(db, uid: str, stock: Stock, price: float = 10.0) -> PriceAlertRule:
    row = PriceAlertRule(
        user_id=uid,
        stock_id=stock.id,
        name=f"{stock.symbol} 提醒",
        enabled=True,
        condition_group={"op": "and", "items": [{"type": "price", "op": ">=", "value": price}]},
    )
    db.add(row)
    db.commit()
    return row


def _body(stock_id: int) -> PriceAlertCreate:
    return PriceAlertCreate(
        stock_id=stock_id,
        condition_group=AlertConditionGroup(
            items=[AlertConditionItem(type="price", op=">=", value=10)]
        ),
    )


def _race(maker, a, b):
    """两个独立 session 在屏障后同时执行；返回 {'a': 结果/异常, 'b': 结果/异常}。"""
    barrier = threading.Barrier(2, timeout=15)
    out: dict[str, object] = {}

    def run(key, fn):
        s = maker()
        try:
            barrier.wait()
            out[key] = fn(s)
        except BaseException as exc:  # noqa: BLE001 - 断言要看异常本身
            out[key] = exc
        finally:
            s.close()

    threads = [threading.Thread(target=run, args=("a", a)), threading.Thread(target=run, args=("b", b))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert len(out) == 2, f"并发线程没跑完: {out}"
    return out


def _ok(res) -> bool:
    return res is not None and not isinstance(res, BaseException)


def test_pg_api并发上限边界只有一个成功(factory):
    maker, s = factory
    _user(s, "cg1")
    stocks = [_stock(s, "cg1", f"61{i:04d}") for i in range(LIMIT)]
    for st in stocks[: LIMIT - 1]:
        _rule(s, "cg1", st)
    user = _user(s, "cg1")
    assert s.query(PriceAlertRule).filter(PriceAlertRule.user_id == "cg1").count() == LIMIT - 1

    out = _race(
        maker,
        lambda db: create_alert_rule(_body(stocks[2].id), db=db, user=user),
        lambda db: create_alert_rule(_body(stocks[2].id), db=db, user=user),
    )

    wins = [k for k, v in out.items() if _ok(v)]
    losses = [k for k, v in out.items() if isinstance(v, HTTPException)]
    assert len(wins) == 1, f"应只有一个并发创建成功，实际: {out}"
    assert len(losses) == 1, f"另一个必须是 403，实际: {out}"
    assert out[losses[0]].status_code == 403
    assert "预警上限" in str(out[losses[0]].detail)

    s.expire_all()
    n = s.query(PriceAlertRule).filter(PriceAlertRule.user_id == "cg1").count()
    assert n == LIMIT, f"并发后规则数必须停在上限 {LIMIT}，实际 {n}"


def test_pg_agent动作并发上限边界只有一个成功(factory):
    maker, s = factory
    _user(s, "cg2")
    stocks = [_stock(s, "cg2", f"62{i:04d}") for i in range(LIMIT)]
    for st in stocks[: LIMIT - 1]:
        _rule(s, "cg2", st)
    a1 = act.propose(s, user_id="cg2", kind="alert", payload={"symbol": "629999", "price": 30})
    a2 = act.propose(s, user_id="cg2", kind="alert", payload={"symbol": "629999", "price": 31})

    out = _race(
        maker,
        lambda db: act.apply_action(db, user_id="cg2", action_id=a1["id"]),
        lambda db: act.apply_action(db, user_id="cg2", action_id=a2["id"]),
    )

    ok = [k for k, v in out.items() if _ok(v)]
    bad = [k for k, v in out.items() if isinstance(v, act.ActionError)]
    assert len(ok) == 1, f"AI 动作并发应只成功一个，实际: {out}"
    assert len(bad) == 1 and int(out[bad[0]].status_code) == 403, f"另一个必须 403，实际: {out}"

    s.expire_all()
    n = s.query(PriceAlertRule).filter(PriceAlertRule.user_id == "cg2").count()
    assert n == LIMIT, f"AI 路径并发后不能超卖，实际 {n}"
    applied = s.query(CopilotAction).filter(CopilotAction.user_id == "cg2", CopilotAction.status == "applied").all()
    assert len(applied) == 1, "被拒的那笔动作不能变成 applied"


def test_pg_api与agent并发共用同一配额(factory):
    maker, s = factory
    _user(s, "cg3")
    stocks = [_stock(s, "cg3", f"63{i:04d}") for i in range(LIMIT)]
    for st in stocks[: LIMIT - 1]:
        _rule(s, "cg3", st)
    user = _user(s, "cg3")
    item = act.propose(s, user_id="cg3", kind="alert", payload={"symbol": "639999", "price": 30})

    out = _race(
        maker,
        lambda db: create_alert_rule(_body(stocks[2].id), db=db, user=user),
        lambda db: act.apply_action(db, user_id="cg3", action_id=item["id"]),
    )

    ok = [k for k, v in out.items() if _ok(v)]
    assert len(ok) == 1, f"API 与 AI 必须共用一把配额闸门，实际: {out}"
    s.expire_all()
    n = s.query(PriceAlertRule).filter(PriceAlertRule.user_id == "cg3").count()
    assert n == LIMIT, f"混合并发后不能超卖，实际 {n}"


def test_pg_自选配额并发不超卖(factory):
    maker, s = factory
    _user(s, "cg4")
    limit = free_tier.member_watchlist_max(s)
    for i in range(limit - 1):
        _stock(s, "cg4", f"64{i:04d}")
    user = _user(s, "cg4")

    from src.core.permissions import check_watchlist_quota

    def add(db, symbol):
        check_watchlist_quota(db, user)
        db.add(Stock(user_id="cg4", symbol=symbol, name=symbol, market="CN", sort_order=0))
        db.commit()
        return True

    out = _race(maker, lambda db: add(db, "649998"), lambda db: add(db, "649999"))
    wins = [k for k, v in out.items() if _ok(v)]
    assert len(wins) == 1, f"自选配额并发应只成功一个，实际: {out}"
    s.expire_all()
    n = s.query(Stock).filter(Stock.user_id == "cg4").count()
    assert n == limit, f"自选数必须停在上限 {limit}，实际 {n}"


def test_pg_lock_quota_row确实取了行锁(factory):
    maker, s = factory
    from src.core.permissions import lock_quota_row

    _user(s, "cg6")
    user = _user(s, "cg6")
    assert lock_quota_row(s, user) is True, "PG 上必须真的取到 users 行锁"
    s.rollback()
    assert lock_quota_row(s, "no-such-user") is False, "用户行不存在时不许谎称加锁"
    s.rollback()
