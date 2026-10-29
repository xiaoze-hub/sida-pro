"""跨租户持仓读写越权回归(2026-10-29 复审阻断项)。

实测高危(本文件复现并锁定):
  1. `load_portfolio_for_stock(stock_id)` 不按当前 user_id 过滤, 而 `trigger_agent_for_stock`
     已经拿到 user_id 却没往下传 → 手动触发的 AI 分析可以读到**他人**持仓。
  2. positions 端点(list/update/defense/delete/reorder)只校验 `Position.user_id`,
     不校验父 `Account.user_id` → A 能读/改/删 B 私有账户里 `user_id=NULL` 的持仓,
     也能看到共享账户里 B 建的持仓。
  3. `/export/portfolio` 与 chat 的 `_build_portfolio_context` 是同一缺口。

本文件锁定的口径:
  - 持仓可见 = **父账户可见 AND 持仓自身可见**; 二者缺一即不可见(404, 不泄露存在性)。
  - 写操作区分 owner / member / 共享(user_id=NULL): 共享项仅 owner 可写(403), 他人私有项 404。
  - 没有用户会话时 fail-closed: 只对应历史共享桶(NULL/NULL), 绝不默默继承全部账户。

角色 × 账户 × 持仓矩阵见 `_tenancy` fixture。
"""

from __future__ import annotations

import csv
import io
import uuid

import pytest
from fastapi.testclient import TestClient

from src.web.database import SessionLocal, init_db
from src.web.models import Account, Position, Stock, User

PREFIX = "xt"


# ---------------------------------------------------------------- fixtures


@pytest.fixture(scope="module", autouse=True)
def _db_ready():
    init_db()


@pytest.fixture()
def client():
    from src.web.app import app

    return TestClient(app)


def _mk_user(db, role: str) -> User:
    user = User(
        id=str(uuid.uuid4()),
        username=f"{PREFIX}_{role}_{uuid.uuid4().hex[:10]}",
        password_hash="not-a-real-hash",  # 测试只走 create_token, 不做口令登录
        role=role,
        is_active=True,
        token_version=1,
    )
    db.add(user)
    db.commit()
    return user


def _mk_account(db, *, user_id, name: str) -> Account:
    acc = Account(name=name, available_funds=10000.0, enabled=True, user_id=user_id)
    db.add(acc)
    db.commit()
    return acc


def _mk_position(db, *, acc, symbol: str, user_id, cost=10.0, qty=100) -> Position:
    stock = Stock(symbol=symbol, name=symbol, market="CN", user_id=None)
    db.add(stock)
    db.flush()
    pos = Position(
        account_id=acc.id,
        stock_id=stock.id,
        cost_price=cost,
        quantity=qty,
        user_id=user_id,
    )
    db.add(pos)
    db.commit()
    return pos


@pytest.fixture()
def tenancy(client):
    """角色(member×2 + owner) × 账户(私有×2 + 共享) × 持仓(本人/他人/NULL 错位)。"""
    from src.web.api.auth import create_token

    db = SessionLocal()
    tag = uuid.uuid4().hex[:6]
    created = {"users": [], "accounts": [], "positions": [], "stocks": []}
    try:
        a = _mk_user(db, "member")
        b = _mk_user(db, "member")
        o = _mk_user(db, "owner")
        created["users"] = [a.id, b.id, o.id]

        acc_a = _mk_account(db, user_id=a.id, name=f"{PREFIX}-A-{tag}")
        acc_b = _mk_account(db, user_id=b.id, name=f"{PREFIX}-B-{tag}")
        acc_shared = _mk_account(db, user_id=None, name=f"{PREFIX}-SHARED-{tag}")
        created["accounts"] = [acc_a.id, acc_b.id, acc_shared.id]

        def sym(n: int) -> str:
            return f"XT{tag}{n}"

        pos = {
            # A 自己账户里的自有持仓: 可读可写
            "a_own": _mk_position(db, acc=acc_a, symbol=sym(1), user_id=a.id),
            # B 私有账户里 user_id=NULL 的持仓: 修复前 A 能读/改/删
            "b_null": _mk_position(db, acc=acc_b, symbol=sym(2), user_id=None),
            # B 私有账户里 B 的持仓
            "b_own": _mk_position(db, acc=acc_b, symbol=sym(3), user_id=b.id),
            # 共享账户里 B 建的持仓: 修复前 A 也能看到
            "shared_b": _mk_position(db, acc=acc_shared, symbol=sym(4), user_id=b.id),
            # 共享账户里的历史 NULL 持仓: 所有人可读, 仅 owner 可写
            "shared_null": _mk_position(db, acc=acc_shared, symbol=sym(5), user_id=None),
            # 数据错位: A 的账户里混入 B 的持仓 → A 也不得读写
            "a_foreign": _mk_position(db, acc=acc_a, symbol=sym(6), user_id=b.id),
        }
        created["positions"] = [p.id for p in pos.values()]
        created["stocks"] = [p.stock_id for p in pos.values()]

        tokens = {}
        for name, user in (("a", a), ("b", b), ("o", o)):
            token, _exp = create_token(user)
            tokens[name] = token

        yield {
            "tag": tag,
            "users": {"a": a, "b": b, "o": o},
            "accounts": {"a": acc_a, "b": acc_b, "shared": acc_shared},
            "pos": pos,
            "tokens": tokens,
        }
    finally:
        db.rollback()
        db.query(Position).filter(Position.id.in_(created["positions"])).delete(
            synchronize_session=False
        )
        db.query(Account).filter(Account.id.in_(created["accounts"])).delete(
            synchronize_session=False
        )
        db.query(Stock).filter(Stock.id.in_(created["stocks"])).delete(
            synchronize_session=False
        )
        from tests.conftest import purge_users

        purge_users(db, ids=created["users"])
        db.commit()
        db.close()


def _hdr(tokens: dict, who: str) -> dict:
    return {"Authorization": f"Bearer {tokens[who]}"}


def _pos_ids(resp) -> set[int]:
    assert resp.status_code == 200, resp.text
    return {row["id"] for row in resp.json()["data"]}


# ---------------------------------------------------------------- 读: list


def test_list_positions_hides_foreign_account_rows(client, tenancy):
    """A 只看得到"账户可见 AND 持仓可见"的持仓, 他人私有账户里 NULL 所有权的持仓也不许出现。"""
    r = client.get("/api/positions", headers=_hdr(tenancy["tokens"], "a"))
    ids = _pos_ids(r)
    p = tenancy["pos"]
    assert p["a_own"].id in ids
    assert p["shared_null"].id in ids
    assert p["b_null"].id not in ids, "B 私有账户里 user_id=NULL 的持仓不得被 A 读到"
    assert p["b_own"].id not in ids
    assert p["shared_b"].id not in ids, "共享账户里 B 建的持仓不得被 A 读到"
    assert p["a_foreign"].id not in ids, "账户归 A 但持仓归 B(数据错位)也不得给 A 读"


def test_list_positions_scoped_by_account_id(client, tenancy):
    """按 account_id 过滤也不能绕过父账户归属。"""
    p = tenancy["pos"]
    r = client.get(
        f"/api/positions?account_id={tenancy['accounts']['b'].id}",
        headers=_hdr(tenancy["tokens"], "a"),
    )
    assert p["b_null"].id not in _pos_ids(r)
    assert p["b_own"].id not in _pos_ids(r)


# ---------------------------------------------------------------- 写: update / defense / delete / reorder


def test_update_position_in_foreign_account_is_404(client, tenancy):
    """B 私有账户里 NULL 所有权持仓: A 改不动, 且拿 404(不泄露存在性)。"""
    p = tenancy["pos"]["b_null"]
    r = client.put(
        f"/api/positions/{p.id}",
        json={"cost_price": 1.0},
        headers=_hdr(tenancy["tokens"], "a"),
    )
    assert r.status_code == 404, r.text

    db = SessionLocal()
    try:
        assert db.get(Position, p.id).cost_price == pytest.approx(10.0)
    finally:
        db.close()


def test_defense_line_in_foreign_account_is_404(client, tenancy):
    p = tenancy["pos"]["b_null"]
    r = client.put(
        f"/api/positions/{p.id}/defense",
        json={"defense_price": 9.0},
        headers=_hdr(tenancy["tokens"], "a"),
    )
    assert r.status_code == 404, r.text

    db = SessionLocal()
    try:
        assert db.get(Position, p.id).defense_price is None
    finally:
        db.close()


def test_delete_position_in_foreign_account_is_404(client, tenancy):
    p = tenancy["pos"]["b_null"]
    r = client.delete(f"/api/positions/{p.id}", headers=_hdr(tenancy["tokens"], "a"))
    assert r.status_code == 404, r.text

    db = SessionLocal()
    try:
        assert db.get(Position, p.id) is not None, "越权删除必须未生效"
    finally:
        db.close()


def test_reorder_skips_foreign_and_shared_positions(client, tenancy):
    """批量排序: 只改自己可写的持仓, 他人私有/共享 NULL 持仓原封不动。"""
    p = tenancy["pos"]
    r = client.put(
        "/api/positions/reorder/batch",
        json={
            "items": [
                {"id": p["a_own"].id, "sort_order": 7},
                {"id": p["b_null"].id, "sort_order": 8},
                {"id": p["shared_b"].id, "sort_order": 9},
            ]
        },
        headers=_hdr(tenancy["tokens"], "a"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["updated"] == 1

    db = SessionLocal()
    try:
        assert db.get(Position, p["a_own"].id).sort_order == 7
        assert db.get(Position, p["b_null"].id).sort_order != 8
        assert db.get(Position, p["shared_b"].id).sort_order != 9
    finally:
        db.close()


def test_member_cannot_write_shared_null_position_but_owner_can(client, tenancy):
    """共享(user_id=NULL)持仓: member 写 403(不得默默继承全部账户), owner 可写。"""
    p = tenancy["pos"]["shared_null"]
    r = client.put(
        f"/api/positions/{p.id}/defense",
        json={"defense_price": 8.5},
        headers=_hdr(tenancy["tokens"], "a"),
    )
    assert r.status_code == 403, r.text

    r2 = client.put(
        f"/api/positions/{p.id}/defense",
        json={"defense_price": 8.5},
        headers=_hdr(tenancy["tokens"], "o"),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["defense_price"] == pytest.approx(8.5)


def test_owner_has_no_privilege_over_other_users_private_rows(client, tenancy):
    """owner 只对共享项有写权, 不是跨用户超级权限。"""
    p = tenancy["pos"]["b_null"]
    r = client.put(
        f"/api/positions/{p.id}",
        json={"cost_price": 2.0},
        headers=_hdr(tenancy["tokens"], "o"),
    )
    assert r.status_code == 404, r.text


def test_member_cannot_delete_or_update_shared_account(client, tenancy):
    """全局共享账户(account.user_id=NULL)只读对所有人, 写仅 owner —— 删除会带走他人持仓。"""
    acc = tenancy["accounts"]["shared"]
    r = client.delete(f"/api/accounts/{acc.id}", headers=_hdr(tenancy["tokens"], "a"))
    assert r.status_code == 403, r.text

    r2 = client.put(
        f"/api/accounts/{acc.id}",
        json={"name": "hijacked"},
        headers=_hdr(tenancy["tokens"], "a"),
    )
    assert r2.status_code == 403, r2.text

    db = SessionLocal()
    try:
        assert db.get(Account, acc.id) is not None
        assert db.get(Account, acc.id).name != "hijacked"
    finally:
        db.close()


def test_foreign_private_account_is_invisible_on_account_endpoints(client, tenancy):
    acc = tenancy["accounts"]["b"]
    hdr = _hdr(tenancy["tokens"], "a")
    assert client.get(f"/api/accounts/{acc.id}", headers=hdr).status_code == 404
    assert client.put(f"/api/accounts/{acc.id}", json={"name": "x"}, headers=hdr).status_code == 404
    assert client.delete(f"/api/accounts/{acc.id}", headers=hdr).status_code == 404
    listed = client.get("/api/accounts", headers=hdr)
    assert listed.status_code == 200
    assert acc.id not in {a["id"] for a in listed.json()["data"]}


# ---------------------------------------------------------------- 导出 / 汇总


def test_export_portfolio_only_contains_visible_rows(client, tenancy, monkeypatch):
    """/export/portfolio 与列表同口径: 不得把他人持仓写进 CSV。"""
    from src.web.api import export as export_api

    monkeypatch.setattr(export_api, "_fetch_quotes_for_stocks", lambda stocks: {})
    monkeypatch.setattr(export_api, "get_hkd_cny_rate", lambda: 0.92)
    monkeypatch.setattr(export_api, "get_usd_cny_rate", lambda: 7.25)

    r = client.get("/api/export/portfolio", headers=_hdr(tenancy["tokens"], "a"))
    assert r.status_code == 200, r.text
    rows = list(csv.reader(io.StringIO(r.content.decode("utf-8-sig"))))
    symbols = {row[0] for row in rows[1:]}
    tag = tenancy["tag"]
    assert f"XT{tag}1" in symbols
    assert f"XT{tag}5" in symbols
    assert f"XT{tag}2" not in symbols, "B 私有账户里的 NULL 持仓不得被导出"
    assert f"XT{tag}4" not in symbols, "共享账户里 B 的持仓不得被导出"
    assert f"XT{tag}6" not in symbols


def test_summary_isolates_foreign_account_rows(client, tenancy):
    r = client.get(
        "/api/portfolio/summary?include_quotes=false",
        headers=_hdr(tenancy["tokens"], "a"),
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    ids = {p["id"] for acc in data["accounts"] for p in acc["positions"]}
    p = tenancy["pos"]
    assert ids == {p["a_own"].id, p["shared_null"].id}


# ---------------------------------------------------------------- chat 上下文 / runtime


def test_chat_portfolio_context_is_scoped(tenancy):
    from src.web.api.chat import _build_portfolio_context

    db = SessionLocal()
    try:
        text = _build_portfolio_context(db, user=tenancy["users"]["a"])
    finally:
        db.close()
    tag = tenancy["tag"]
    assert f"XT{tag}1" in text
    assert f"XT{tag}5" in text
    for hidden in (2, 3, 4, 6):
        assert f"XT{tag}{hidden}" not in text, f"XT{tag}{hidden} 是他人持仓, 不得进入 AI 上下文"


def test_chat_portfolio_context_fails_closed_without_user(tenancy):
    """无会话用户时不得退回"全部持仓", 只允许空上下文。"""
    from src.web.api.chat import _build_portfolio_context

    db = SessionLocal()
    try:
        text = _build_portfolio_context(db, user=None)
    finally:
        db.close()
    assert text == "", "缺用户会话时必须空上下文, 不得默默继承全部账户"


def test_load_portfolio_for_stock_is_scoped_by_trigger_user(tenancy, monkeypatch):
    """AI 单只分析上下文: 传谁的用户就只给谁的持仓(修复前返回全部账户)。"""
    import src.bootstrap.runtime as rt
    from sqlalchemy.orm import sessionmaker

    TestingSession = sessionmaker(bind=SessionLocal().get_bind(), expire_on_commit=False)
    monkeypatch.setattr(rt, "SessionLocal", TestingSession)

    p = tenancy["pos"]["b_own"]
    stock_id = p.stock_id
    ctx = rt.load_portfolio_for_stock(stock_id, tenancy["users"]["a"].id)
    symbols = {pos.symbol for acc in ctx.accounts for pos in acc.positions}
    assert symbols == set(), "A 对这只票没有任何持仓, 不得读到 B 的"


def test_load_portfolio_for_stock_keeps_own_positions(tenancy, monkeypatch):
    import src.bootstrap.runtime as rt
    from sqlalchemy.orm import sessionmaker

    TestingSession = sessionmaker(bind=SessionLocal().get_bind(), expire_on_commit=False)
    monkeypatch.setattr(rt, "SessionLocal", TestingSession)

    p = tenancy["pos"]["a_own"]
    ctx = rt.load_portfolio_for_stock(p.stock_id, tenancy["users"]["a"].id)
    symbols = {pos.symbol for acc in ctx.accounts for pos in acc.positions}
    assert symbols == {p.stock.symbol}


def test_load_portfolio_for_stock_without_user_uses_stock_tenant(tenancy, monkeypatch):
    """系统调度没有会话: 按标的归属取租户, 不得退化成全表。"""
    import src.bootstrap.runtime as rt
    from sqlalchemy.orm import sessionmaker

    db = SessionLocal()
    try:
        stock = db.get(Stock, tenancy["pos"]["b_own"].stock_id)
        stock.user_id = tenancy["users"]["b"].id
        db.commit()
        b_own_symbol = stock.symbol
    finally:
        db.close()

    TestingSession = sessionmaker(bind=SessionLocal().get_bind(), expire_on_commit=False)
    monkeypatch.setattr(rt, "SessionLocal", TestingSession)

    ctx = rt.load_portfolio_for_stock(tenancy["pos"]["b_own"].stock_id)
    symbols = {pos.symbol for acc in ctx.accounts for pos in acc.positions}
    assert symbols == {b_own_symbol}, "无会话时必须按标的归属收敛, 不能把所有人的持仓都给它"


def test_trigger_agent_for_stock_passes_user_id_to_portfolio(tenancy, monkeypatch):
    """trigger_agent_for_stock 已拿到 user_id: 必须显式传给 load_portfolio_for_stock。

    系统调度(user_id=None)不能传成"显式共享桶", 而要传内部缺省哨兵, 由
    load_portfolio_for_stock 按标的归属收敛 —— 两条路径都不允许回到"全部账户"。
    """
    import asyncio
    from types import SimpleNamespace

    import src.bootstrap.runtime as rt

    captured: dict = {}

    def _spy(stock_id, user_id=None):
        captured["stock_id"] = stock_id
        captured["user_id"] = user_id
        raise RuntimeError("stop-after-portfolio")

    monkeypatch.setattr(rt, "load_portfolio_for_stock", _spy)
    agent_name = next(iter(rt.AGENT_REGISTRY))
    stock = SimpleNamespace(id=12345, symbol="XTSPY", name="spy", market="CN")

    with pytest.raises(RuntimeError):
        asyncio.run(
            rt.trigger_agent_for_stock(
                agent_name, stock, user_id=tenancy["users"]["a"].id
            )
        )
    assert captured == {"stock_id": 12345, "user_id": tenancy["users"]["a"].id}

    # 无会话(系统调度): 必须传"未指定"哨兵, 而不是 None(显式共享桶)
    with pytest.raises(RuntimeError):
        asyncio.run(rt.trigger_agent_for_stock(agent_name, stock))
    assert captured["user_id"] is rt._UNSET
