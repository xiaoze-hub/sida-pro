"""七审回归(2026-10-29): 删除自选的并发级联 + adopt 并发同 symbol。

独立实审 B2: `DELETE /api/stocks/{id}` 的预检和删除在两条语句上, 中间窗口里
另一个事务给本行插了**他人的** positions / price_alert_rules / price_alert_hits,
旧实现照样 200 删成功, 数据库 `ON DELETE CASCADE` 把这些行一起删掉 —— 预检结论
在提交时已过期, 且用户没有任何报错。SQLite 上无法复现(PG 才有行锁/FK 锁语义),
所以竞态用例只在真实 PG16 上跑, 首尾用 `is_postgres()` 显式跳过。

修复口径(`stocks.delete_stock`):
- 第一件事对本行 `SELECT ... FOR UPDATE`; PG 里任何引用 stocks.id 的 INSERT
  都要先取该行的 FOR KEY SHARE 锁, 与本锁冲突 → 并发写入被挡到本事务结束,
  之后它拿 FK 冲突(23503), 而不是"写成功再被静默级联删";
- 删除语句本身再带一次归属条件, 挡"预检后行归属被改"的 TOCTOU;
- uid 缺失直接 403(fail-closed), 不靠 SQL 三值逻辑;
- 本人命中若挂在**已迁走/他人**的规则上, 一律 409, 不随本行级联消失。

adopt: 同一共享行加同样的 FOR UPDATE(与删除互斥), 目标私有自选用 SAVEPOINT +
重查做幂等; `user_id` 缺失 fail-closed, NULL 历史规则谁也不能认领。
"""

from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError, OperationalError

from src.db.models import (
    Account,
    Position,
    PriceAlertHit,
    PriceAlertRule,
    Stock,
    StockAgent,
    User,
)
from src.db.session import engine
from src.web.database import SessionLocal, init_db
from src.web.api._db_errors import sqlstate

PREFIX = "r7"


@pytest.fixture(scope="module", autouse=True)
def _db_ready():
    init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    tag = uuid.uuid4().hex[:6]
    created = {"users": [], "stocks": [], "accounts": [], "positions": [],
               "rules": [], "hits": [], "agents": []}

    def user(role="member", name="u"):
        u = User(id=str(uuid.uuid4()), username=f"{PREFIX}_{name}_{tag}_{uuid.uuid4().hex[:4]}",
                 password_hash="x", role=role, is_active=True, token_version=1)
        s.add(u)
        s.commit()
        created["users"].append(u.id)
        return u

    def stock(uid=None, symbol=None, market="CN"):
        st = Stock(symbol=symbol or f"R{uuid.uuid4().hex[:7].upper()}", name="七审测试",
                   market=market, user_id=uid)
        s.add(st)
        s.commit()
        created["stocks"].append(st.id)
        return st

    def account(uid):
        a = Account(name=f"acc-{uuid.uuid4().hex[:6]}", available_funds=0.0, enabled=True, user_id=uid)
        s.add(a)
        s.commit()
        created["accounts"].append(a.id)
        return a

    def rule(st, uid, name="r"):
        r = PriceAlertRule(user_id=uid, stock_id=st.id, name=name,
                           condition_group={"op": "and", "items": []}, enabled=True)
        s.add(r)
        s.commit()
        created["rules"].append(r.id)
        return r

    def hit(r, st):
        h = PriceAlertHit(rule_id=r.id, stock_id=st.id, trigger_bucket=uuid.uuid4().hex[:12])
        s.add(h)
        s.commit()
        created["hits"].append(h.id)
        return h

    ctx = SimpleNamespace(s=s, user=user, stock=stock, account=account, rule=rule,
                          hit=hit, created=created)
    try:
        yield ctx
    finally:
        s.rollback()
        s.query(PriceAlertHit).filter(PriceAlertHit.stock_id.in_(created["stocks"])).delete(
            synchronize_session=False)
        s.query(PriceAlertRule).filter(PriceAlertRule.stock_id.in_(created["stocks"])).delete(
            synchronize_session=False)
        s.query(StockAgent).filter(StockAgent.stock_id.in_(created["stocks"])).delete(
            synchronize_session=False)
        s.query(Position).filter(Position.stock_id.in_(created["stocks"])).delete(
            synchronize_session=False)
        s.query(Stock).filter(Stock.id.in_(created["stocks"])).delete(synchronize_session=False)
        if created["users"]:
            own = [row[0] for row in s.query(Stock.id).filter(Stock.user_id.in_(created["users"])).all()]
            if own:
                s.query(PriceAlertHit).filter(PriceAlertHit.stock_id.in_(own)).delete(
                    synchronize_session=False)
                s.query(PriceAlertRule).filter(PriceAlertRule.stock_id.in_(own)).delete(
                    synchronize_session=False)
                s.query(StockAgent).filter(StockAgent.stock_id.in_(own)).delete(
                    synchronize_session=False)
                s.query(Stock).filter(Stock.id.in_(own)).delete(synchronize_session=False)
        if created["accounts"]:
            s.query(Position).filter(Position.account_id.in_(created["accounts"])).delete(
                synchronize_session=False)
            s.query(Account).filter(Account.id.in_(created["accounts"])).delete(
                synchronize_session=False)
        s.commit()
        from tests.conftest import purge_users

        purge_users(s, ids=created["users"])
        s.commit()
        s.close()


def app():
    from src.web.app import app as _app

    return _app


def _hdr(u: User) -> dict:
    from src.web.api.auth import create_token

    tok, _ = create_token(u)
    return {"Authorization": f"Bearer {tok}"}


def _pg_only():
    from src.db.dialect import is_postgres

    if not is_postgres():
        pytest.skip("竞态/FK 锁语义需真实 PG 隔离库")


def _insert_with_lock_timeout(fn, lock_ms="700ms"):
    """在独立事务里跑 fn(session); 期待锁等待超时(55P03)。"""
    s = SessionLocal()
    try:
        s.execute(text(f"SET LOCAL lock_timeout = '{lock_ms}'"))
        fn(s)
        s.commit()
        return None
    except OperationalError as exc:
        s.rollback()
        return exc
    finally:
        s.close()


# ───────── 1. FOR UPDATE 与所有引用路径的 FK 锁语义对拍(真实 PG) ─────────


def test_delete_lock_blocks_foreign_rule_position_hit_agent_inserts(db):
    _pg_only()
    member = db.user("member", "m")
    other = db.user("member", "o")
    st = db.stock(member.id)
    stock_id = st.id

    lock_seen = threading.Event()
    release = threading.Event()
    main_ident = threading.get_ident()
    delete_code: dict[str, int] = {}

    def _after_execute(conn, cursor, statement, parameters, context, executemany):
        # 只在删自选的工作线程里、且是在拿到 stock 行锁之后暂停
        if threading.get_ident() == main_ident:
            return
        upper = statement.upper()
        if "FOR UPDATE" in upper and "FROM STOCKS" in upper and not lock_seen.is_set():
            lock_seen.set()
            release.wait(20)

    def _do_delete():
        c = TestClient(app(), raise_server_exceptions=False)
        delete_code["status"] = c.delete(f"/api/stocks/{stock_id}", headers=_hdr(member)).status_code

    event.listen(engine, "after_cursor_execute", _after_execute)
    t = threading.Thread(target=_do_delete, daemon=True)
    try:
        t.start()
        assert lock_seen.wait(15), "删除请求没有对 stock 行取 FOR UPDATE"

        # ① 他人规则; ② 他人持仓; ③ 命中(stock_id 指向本行); ④ Agent 绑定
        def _rule(s2):
            s2.add(PriceAlertRule(user_id=other.id, stock_id=stock_id, name="race",
                                  condition_group={"op": "and", "items": []}))
            s2.flush()

        acc = db.account(other.id)
        other_stock = db.stock(other.id)

        def _position(s2):
            s2.add(Position(account_id=acc.id, stock_id=stock_id, cost_price=1.0,
                            quantity=100, user_id=other.id))
            s2.flush()

        def _hit(s2):
            r = PriceAlertRule(user_id=other.id, stock_id=other_stock.id, name="h",
                               condition_group={"op": "and", "items": []})
            s2.add(r)
            s2.flush()
            s2.add(PriceAlertHit(rule_id=r.id, stock_id=stock_id,
                                 trigger_bucket=uuid.uuid4().hex[:12]))
            s2.flush()

        def _agent(s2):
            s2.add(StockAgent(stock_id=stock_id, agent_name=f"wf_{uuid.uuid4().hex[:6]}"))
            s2.flush()

        for label, fn in (("rule", _rule), ("position", _position), ("hit", _hit), ("agent", _agent)):
            exc = _insert_with_lock_timeout(fn)
            assert exc is not None, f"{label} 插入没有被 stock 行锁挡住 —— FK 锁语义不成立"
            assert sqlstate(exc) == "55P03", (label, sqlstate(exc), str(exc)[:200])
    finally:
        release.set()
        t.join(30)
        event.remove(engine, "after_cursor_execute", _after_execute)

    assert delete_code.get("status") == 200, delete_code

    # 删除提交后, 之前被挡住的那条规则只能拿到 FK 冲突(23503), 不能"写成功再被删"
    def _late(s2):
        s2.add(PriceAlertRule(user_id=other.id, stock_id=stock_id, name="late",
                              condition_group={"op": "and", "items": []}))
        s2.flush()

    s3 = SessionLocal()
    try:
        with pytest.raises(IntegrityError) as ei:
            _late(s3)
            s3.commit()
        assert sqlstate(ei.value) == "23503", sqlstate(ei.value)
    finally:
        s3.rollback()
        s3.close()

    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == stock_id).first() is None
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.stock_id == stock_id).count() == 0
    assert db.s.query(Position).filter(Position.stock_id == stock_id).count() == 0


# ───────── 2. 已提交的他人规则: 删除必须 409 且全部保留 ─────────


def test_foreign_rule_committed_before_delete_is_preserved(db):
    owner = db.user("owner", "o")
    member = db.user("member", "m")
    st = db.stock(None)
    r = db.rule(st, member.id)
    h = db.hit(r, st)

    c = TestClient(app())
    resp = c.delete(f"/api/stocks/{st.id}", headers=_hdr(owner))
    assert resp.status_code == 409, resp.text
    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == st.id).first() is not None
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == r.id).first() is not None
    assert db.s.query(PriceAlertHit).filter(PriceAlertHit.id == h.id).first() is not None


# ───────── 3. 并发压测: 删除与"他人建规则"任意交错都不许丢他人数据 ─────────


def test_delete_vs_foreign_rule_race_never_loses_data(db):
    _pg_only()
    owner = db.user("owner", "o")
    member = db.user("member", "m")

    for _ in range(8):
        st = db.stock(None)
        stock_id = st.id
        result: dict[str, object] = {}

        def _delete():
            c = TestClient(app(), raise_server_exceptions=False)
            result["delete"] = c.delete(f"/api/stocks/{stock_id}", headers=_hdr(owner)).status_code

        def _create():
            c = TestClient(app(), raise_server_exceptions=False)
            resp = c.post("/api/price-alerts", headers=_hdr(member), json={
                "stock_id": stock_id, "name": "race",
                "condition_group": {"op": "and",
                                    "items": [{"type": "price", "op": ">=", "value": 1.0}]},
            })
            result["create"] = resp.status_code

        t1 = threading.Thread(target=_delete)
        t2 = threading.Thread(target=_create)
        t1.start()
        t2.start()
        t1.join(30)
        t2.join(30)
        db.s.expire_all()
        stock_left = db.s.query(Stock).filter(Stock.id == stock_id).first() is not None
        rules = db.s.query(PriceAlertRule).filter(PriceAlertRule.stock_id == stock_id).all()
        codes = (result.get("delete"), result.get("create"))
        assert 500 not in codes, codes
        if result.get("create") == 200:
            # 规则建成了 → 自选行必须还在, 且规则一条不少
            assert stock_left, codes
            assert len(rules) == 1, codes
        if result.get("delete") == 200:
            # 自选删掉了 → 规则不允许存在(建规则的那一侧必须是 4xx)
            assert not rules, codes
            assert result.get("create") != 200, codes


# ───────── 4. 并发 adopt 同一共享行: 串行化, 只建一条私有自选 ─────────


def test_concurrent_adopt_same_shared_row_single_target(db):
    _pg_only()
    member = db.user("member", "m")
    st = db.stock(None, symbol=f"AD{uuid.uuid4().hex[:6].upper()}")
    r1 = db.rule(st, member.id, "r1")
    r2 = db.rule(st, member.id, "r2")
    h1 = db.hit(r1, st)
    h2 = db.hit(r2, st)

    out: dict[str, object] = {}

    def _adopt(rule_id, key):
        c = TestClient(app(), raise_server_exceptions=False)
        out[key] = c.post(f"/api/price-alerts/{rule_id}/adopt", headers=_hdr(member))

    ts = [threading.Thread(target=_adopt, args=(r1.id, "a")),
          threading.Thread(target=_adopt, args=(r2.id, "b"))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(30)

    for key in ("a", "b"):
        resp = out[key]
        assert resp.status_code == 200, (key, resp.status_code, resp.text[:200])
    db.s.expire_all()
    targets = db.s.query(Stock).filter(
        Stock.user_id == member.id, Stock.symbol == st.symbol
    ).all()
    assert len(targets) == 1, [t.id for t in targets]
    target_id = targets[0].id
    assert {x.stock_id for x in db.s.query(PriceAlertRule).filter(
        PriceAlertRule.id.in_([r1.id, r2.id])).all()} == {target_id}
    assert {x.stock_id for x in db.s.query(PriceAlertHit).filter(
        PriceAlertHit.id.in_([h1.id, h2.id])).all()} == {target_id}


# ───────── 5. unique 冲突恢复: 建私有自选撞唯一索引不留 500/半截事务 ─────────


def test_adopt_unique_conflict_recovered_by_requery(db, monkeypatch):
    """并发下对方刚建好同 symbol 私有自选: INSERT 撞唯一索引 → 重查复用。

    真实时序是"我方 _my_stock 查不到 → 对方提交 → 我方 INSERT 撞唯一索引"。
    这里用 unique 索引 + 第一次查询返回 None 精确复现该时序(SQLite 上没有
    该类索引, 也无法用行锁复现), 验证走的是 SAVEPOINT 回滚 + 重查, 而不是 500。
    """
    _pg_only()
    import src.web.api.price_alerts as pa

    member = db.user("member", "m")
    symbol = f"UQ{uuid.uuid4().hex[:6].upper()}"
    shared = db.stock(None, symbol=symbol)
    existing = db.stock(member.id, symbol=symbol)  # 对方已建好的私有自选
    r = db.rule(shared, member.id, "uq")
    h = db.hit(r, shared)

    db.s.rollback()  # 释放 fixture 会话上的 ACCESS SHARE, 否则 DDL 拿不到排他锁
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS uq_test_stocks_user_symbol_market"))
        conn.execute(text(
            "CREATE UNIQUE INDEX uq_test_stocks_user_symbol_market "
            "ON stocks (user_id, symbol, market) WHERE user_id IS NOT NULL"
        ))
    real = pa._my_stock
    calls = {"n": 0}

    def _fake(db_, uid, sym, market):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # 模拟"预检时还看不到并发提交的新行"
        return real(db_, uid, sym, market)

    monkeypatch.setattr(pa, "_my_stock", _fake)
    try:
        c = TestClient(app(), raise_server_exceptions=False)
        resp = c.post(f"/api/price-alerts/{r.id}/adopt", headers=_hdr(member))
        assert resp.status_code == 200, resp.text
        body = resp.json()["data"]
        assert body["created_stock"] is False
        assert body["stock_id"] == existing.id
        db.s.expire_all()
        assert db.s.query(Stock).filter(
            Stock.user_id == member.id, Stock.symbol == symbol
        ).count() == 1
        assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == r.id).one().stock_id == existing.id
        assert db.s.query(PriceAlertHit).filter(PriceAlertHit.id == h.id).one().stock_id == existing.id
    finally:
        db.s.rollback()
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS uq_test_stocks_user_symbol_market"))


# ───────── 6. NULL 历史规则: 谁也不能认领 ─────────


def test_null_owned_rule_cannot_be_adopted_by_anyone(db):
    owner = db.user("owner", "o")
    member = db.user("member", "m")
    st = db.stock(None)
    r = db.rule(st, None, "legacy")
    h = db.hit(r, st)

    c = TestClient(app())
    for user in (member, owner):
        resp = c.post(f"/api/price-alerts/{r.id}/adopt", headers=_hdr(user))
        assert resp.status_code == 404, resp.text
    db.s.expire_all()
    kept = db.s.query(PriceAlertRule).filter(PriceAlertRule.id == r.id).one()
    assert kept.user_id is None and kept.stock_id == st.id
    assert db.s.query(PriceAlertHit).filter(PriceAlertHit.id == h.id).one().stock_id == st.id
    assert db.s.query(Stock).filter(Stock.user_id.in_([owner.id, member.id])).count() == 0


# ───────── 7. 本人命中挂在"迁走/他人"规则上: 不许被级联删 ─────────


def test_own_hit_on_moved_rule_blocks_delete(db):
    member = db.user("member", "m")
    target = db.stock(member.id, symbol=f"MV{uuid.uuid4().hex[:6].upper()}")
    elsewhere = db.stock(member.id, symbol=f"EL{uuid.uuid4().hex[:6].upper()}")
    r = db.rule(elsewhere, member.id, "moved rule")
    h = db.hit(r, target)  # 规则已迁走, 命中仍指向 target

    c = TestClient(app())
    resp = c.delete(f"/api/stocks/{target.id}", headers=_hdr(member))
    assert resp.status_code == 409, resp.text
    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == target.id).first() is not None
    assert db.s.query(PriceAlertHit).filter(PriceAlertHit.id == h.id).first() is not None


# ───────── 8. 无 user_id 一律 fail-closed ─────────


def test_delete_and_adopt_fail_closed_without_user_id(db):
    from src.web.api.price_alerts import adopt_alert_rule_to_private
    from src.web.api.stocks import delete_stock

    member = db.user("member", "m")
    st = db.stock(member.id)
    r = db.rule(st, None, "legacy2")
    ghost = SimpleNamespace(id=None, role="owner", username="ghost")

    with pytest.raises(Exception) as e1:
        delete_stock(st.id, db=db.s, user=ghost)
    assert getattr(e1.value, "status_code", None) == 403
    with pytest.raises(Exception) as e2:
        adopt_alert_rule_to_private(r.id, db=db.s, user=ghost)
    assert getattr(e2.value, "status_code", None) == 403

    db.s.expire_all()
    assert db.s.query(Stock).filter(Stock.id == st.id).first() is not None
    assert db.s.query(PriceAlertRule).filter(PriceAlertRule.id == r.id).one().user_id is None


# ───────── 9. 拒绝删除要留动作日志, 但日志里不能带归属人信息 ─────────


def test_delete_refusal_logs_action_without_owner_leak(db, caplog):
    owner = db.user("owner", "o")
    member = db.user("member", "m")
    st = db.stock(None)
    db.rule(st, member.id)

    import logging

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        c = TestClient(app())
        resp = c.delete(f"/api/stocks/{st.id}", headers=_hdr(owner))
    assert resp.status_code == 409

    texts = [r.getMessage() for r in caplog.records if r.name.startswith("src.web.api")]
    joined = "\n".join(texts)
    assert "拒绝删除自选" in joined, texts
    # 日志给运维定位用, 不回显归属人 id/用户名
    assert member.id not in joined and member.username not in joined
