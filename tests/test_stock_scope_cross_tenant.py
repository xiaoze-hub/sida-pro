"""自选(Stock)双层作用域回归(2026-10-29 三审 R3)。

修前实测越权:

1. `POST /api/positions` 只按主键取 `Stock` —— A 传 B 的私有自选 `stock_id` 也能建仓,
   持仓挂到 B 的 Stock 行上; B 删自己的自选时 **级联把 A 的持仓删掉**;
2. `DELETE /api/stocks/{id}` 的 `has_position` 查**全库**持仓 —— 有人(哪怕历史错位)
   在我的自选行上有持仓, 我就永远删不掉, 而且报错文案等于告诉我"有人在持有这只票"。

修复口径:

- 标的可见 = 自己的 + 历史共享(NULL); 他人私有 → 与"不存在"同文案(400), 不给存在性信号;
- 删除自选: 自己可见的持仓还在 → 400(对他本人真实且必要); 只有他人持仓引用 →
  409 通用文案(不报数量/账户/归属人), **拒绝删除**(不破坏他人数据)。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from src.db.models import Account, Position, Stock, User
from src.web.database import SessionLocal, init_db

PREFIX = "sto"


@pytest.fixture(scope="module", autouse=True)
def _db_ready():
    init_db()


@pytest.fixture()
def client():
    from src.web.app import app

    return TestClient(app)


@pytest.fixture()
def tenancy():
    """A/B 各一个私有自选 + 一条共享自选; A 有自己的账户。"""
    from src.web.api.auth import create_token

    db = SessionLocal()
    tag = uuid.uuid4().hex[:6]
    created = {"users": [], "stocks": [], "accounts": [], "positions": []}
    try:
        a = User(id=str(uuid.uuid4()), username=f"{PREFIX}_a_{tag}", password_hash="x",
                 role="member", is_active=True, token_version=1)
        b = User(id=str(uuid.uuid4()), username=f"{PREFIX}_b_{tag}", password_hash="x",
                 role="member", is_active=True, token_version=1)
        db.add_all([a, b])
        db.commit()
        created["users"] = [a.id, b.id]

        stock_a = Stock(symbol=f"SA{tag}", name=f"SA{tag}", market="CN", user_id=a.id)
        stock_b = Stock(symbol=f"SB{tag}", name=f"SB{tag}", market="CN", user_id=b.id)
        stock_shared = Stock(symbol=f"SS{tag}", name=f"SS{tag}", market="CN", user_id=None)
        db.add_all([stock_a, stock_b, stock_shared])
        db.commit()
        created["stocks"] = [stock_a.id, stock_b.id, stock_shared.id]

        acc_a = Account(name=f"{PREFIX}-A-{tag}", available_funds=5000.0, enabled=True, user_id=a.id)
        acc_b = Account(name=f"{PREFIX}-B-{tag}", available_funds=5000.0, enabled=True, user_id=b.id)
        db.add_all([acc_a, acc_b])
        db.commit()
        created["accounts"] = [acc_a.id, acc_b.id]

        tokens = {}
        for name, u in (("a", a), ("b", b)):
            tok, _ = create_token(u)
            tokens[name] = tok

        yield {
            "users": {"a": a, "b": b},
            "stocks": {"a": stock_a, "b": stock_b, "shared": stock_shared},
            "accounts": {"a": acc_a, "b": acc_b},
            "tokens": tokens,
            "created": created,
            "db": db,
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


def _hdr(t, who):
    return {"Authorization": f"Bearer {t['tokens'][who]}"}


def _create_position(client, tenancy, who: str, stock_id: int):
    acc = tenancy["accounts"][who]
    return client.post(
        "/api/positions",
        json={"account_id": acc.id, "stock_id": stock_id, "cost_price": 10.0, "quantity": 100},
        headers=_hdr(tenancy, who),
    )


def test_cannot_create_position_on_foreign_private_stock(client, tenancy):
    """A 不能把持仓挂到 B 的私有自选行上(修前可以)。"""
    r = _create_position(client, tenancy, "a", tenancy["stocks"]["b"].id)
    assert r.status_code == 400, r.text
    assert "股票不存在" in r.text

    db = tenancy["db"]
    leaked = (
        db.query(Position)
        .filter(Position.stock_id == tenancy["stocks"]["b"].id)
        .count()
    )
    assert leaked == 0, "跨租户建仓必须一条都不落地"


def test_can_create_position_on_own_and_shared_stock(client, tenancy):
    r_own = _create_position(client, tenancy, "a", tenancy["stocks"]["a"].id)
    assert r_own.status_code == 200, r_own.text
    tenancy["created"]["positions"].append(r_own.json()["data"]["id"])

    r_shared = _create_position(client, tenancy, "a", tenancy["stocks"]["shared"].id)
    assert r_shared.status_code == 200, r_shared.text
    tenancy["created"]["positions"].append(r_shared.json()["data"]["id"])


def test_delete_stock_blocked_by_own_visible_position(client, tenancy):
    """自己的自选上有自己的持仓 → 400, 提示先清理持仓(对他本人是真实信息)。"""
    r = _create_position(client, tenancy, "a", tenancy["stocks"]["a"].id)
    assert r.status_code == 200
    tenancy["created"]["positions"].append(r.json()["data"]["id"])

    d = client.delete(f"/api/stocks/{tenancy['stocks']['a'].id}", headers=_hdr(tenancy, "a"))
    assert d.status_code == 400
    assert "持仓" in d.text


def test_delete_stock_refused_when_only_foreign_position_references_it(client, tenancy):
    """历史错位数据: B 的持仓挂在 A 的自选行上 → A 删不掉, 但也不该拿到归属信息。"""
    db = tenancy["db"]
    pos = Position(
        account_id=tenancy["accounts"]["b"].id,
        stock_id=tenancy["stocks"]["a"].id,
        cost_price=9.0,
        quantity=10,
        user_id=tenancy["users"]["b"].id,
    )
    db.add(pos)
    db.commit()
    tenancy["created"]["positions"].append(pos.id)

    d = client.delete(f"/api/stocks/{tenancy['stocks']['a'].id}", headers=_hdr(tenancy, "a"))
    assert d.status_code == 409, d.text
    payload = d.json()
    detail = str(payload.get("detail") or payload.get("message") or "")
    # 通用文案: 不带数量/账户名/归属人, 也不暗示"有人持有这只票"
    assert "引用" in detail
    assert tenancy["accounts"]["b"].name not in detail
    assert tenancy["users"]["b"].id not in detail
    assert "持仓" not in detail
    # 股票必须还在(没有级联删掉别人的持仓)
    assert db.query(Stock).filter(Stock.id == tenancy["stocks"]["a"].id).first() is not None
    assert db.query(Position).filter(Position.id == pos.id).first() is not None


def test_delete_own_stock_without_positions_works(client, tenancy):
    db = tenancy["db"]
    tag = uuid.uuid4().hex[:6]
    st = Stock(symbol=f"SZ{tag}", name=f"SZ{tag}", market="CN", user_id=tenancy["users"]["a"].id)
    db.add(st)
    db.commit()
    tenancy["created"]["stocks"].append(st.id)

    d = client.delete(f"/api/stocks/{st.id}", headers=_hdr(tenancy, "a"))
    assert d.status_code == 200, d.text
