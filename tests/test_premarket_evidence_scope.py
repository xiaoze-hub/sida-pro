"""盘前证据层作用域回归(2026-10-29 三审 R1 的证据层一半)。

修前实测: `_collect_portfolio_block` / `_collect_fund_structure_block` 用
`if user_id: q.filter(Position.user_id == user_id)` —— **user_id=None 时一条过滤都不加**,
于是"无会话/系统报告"路径把**全库所有租户**的持仓与私有自选读进证据块。共享桶被当成
"全库", 这是 fail-open 的教科书例子。

修复口径(与 `portfolio_scope` 同源):

- 传 uid → 只采该用户可见(账户可见 AND 持仓可见; 自选 = 自己的 + 历史共享);
- 传 None → **显式共享桶**: `Account.user_id IS NULL AND Position.user_id IS NULL`,
  自选 `Stock.user_id IS NULL`; 绝不是全库。
"""

from __future__ import annotations

import uuid

import pytest

import src.core.exday_structure as exday
from src.core import premarket_evidence as pe
from src.db.models import Account, Position, Stock, User
from src.web.database import SessionLocal, init_db

PREFIX = "pms"


@pytest.fixture(scope="module", autouse=True)
def _db_ready():
    init_db()


@pytest.fixture()
def tenancy():
    db = SessionLocal()
    tag = uuid.uuid4().hex[:6]
    created = {"users": [], "accounts": [], "positions": [], "stocks": []}
    try:
        a = User(id=str(uuid.uuid4()), username=f"{PREFIX}_a_{tag}", password_hash="x",
                 role="member", is_active=True, token_version=1)
        b = User(id=str(uuid.uuid4()), username=f"{PREFIX}_b_{tag}", password_hash="x",
                 role="member", is_active=True, token_version=1)
        db.add_all([a, b])
        db.commit()
        created["users"] = [a.id, b.id]

        acc_a = Account(name=f"{PREFIX}-A-{tag}", available_funds=0, enabled=True, user_id=a.id)
        acc_b = Account(name=f"{PREFIX}-B-{tag}", available_funds=0, enabled=True, user_id=b.id)
        acc_shared = Account(name=f"{PREFIX}-S-{tag}", available_funds=0, enabled=True, user_id=None)
        db.add_all([acc_a, acc_b, acc_shared])
        db.commit()
        created["accounts"] = [acc_a.id, acc_b.id, acc_shared.id]

        def stock(uid, sym):
            st = Stock(symbol=sym, name=sym, market="CN", user_id=uid)
            db.add(st)
            db.commit()
            created["stocks"].append(st.id)
            return st

        def pos(acc, uid, sym, qty=100):
            st = stock(uid, sym)
            p = Position(account_id=acc.id, stock_id=st.id, cost_price=10.0, quantity=qty,
                         user_id=uid)
            db.add(p)
            db.commit()
            created["positions"].append(p.id)
            return p

        syms = {
            "a_pos": f"PA{tag}",
            "b_pos": f"PB{tag}",
            "shared_pos": f"PS{tag}",
            "a_watch": f"WA{tag}",
            "b_watch": f"WB{tag}",
            "shared_watch": f"WS{tag}",
        }
        pos(acc_a, a.id, syms["a_pos"])
        pos(acc_b, b.id, syms["b_pos"])
        pos(acc_shared, None, syms["shared_pos"])
        stock(a.id, syms["a_watch"])
        stock(b.id, syms["b_watch"])
        stock(None, syms["shared_watch"])

        yield {"db": db, "users": {"a": a, "b": b}, "syms": syms}
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


def _portfolio_symbols(block) -> set[str]:
    assert block["quality"] in ("ok", "suspect", "missing"), block
    return {it.get("symbol") for it in (block.get("items") or [])}


def test_portfolio_block_scoped_to_user(tenancy):
    block = pe._collect_portfolio_block(tenancy["db"], tenancy["users"]["a"].id, "2026-10-29",
                                        with_quotes=False)
    syms = _portfolio_symbols(block)
    assert tenancy["syms"]["a_pos"] in syms
    assert tenancy["syms"]["b_pos"] not in syms, "证据层不得含他人的成本/仓位"
    assert tenancy["syms"]["shared_pos"] in syms, "历史共享持仓对所有人可见(既有口径)"


def test_portfolio_block_without_user_is_shared_bucket_only(tenancy):
    """user_id=None 必须收敛到共享桶, 不能是全库(修前就是全库)。"""
    block = pe._collect_portfolio_block(tenancy["db"], None, "2026-10-29", with_quotes=False)
    syms = _portfolio_symbols(block)
    assert tenancy["syms"]["shared_pos"] in syms
    assert tenancy["syms"]["a_pos"] not in syms
    assert tenancy["syms"]["b_pos"] not in syms


def test_fund_structure_symbols_scoped(tenancy, monkeypatch):
    """四档结构块取的自选/持仓标的集合同样要按归属收窄。"""
    seen: list[str] = []

    def fake_fetch(symbol, **_k):
        seen.append(symbol)
        return {"available": False, "reason": "test"}

    monkeypatch.setattr(exday, "fetch", fake_fetch)
    pe._collect_fund_structure_block(tenancy["db"], tenancy["users"]["a"].id, "2026-10-29")
    assert tenancy["syms"]["b_pos"] not in seen
    assert tenancy["syms"]["b_watch"] not in seen
    assert tenancy["syms"]["a_pos"] in seen


def test_fund_structure_symbols_without_user_is_shared_bucket(tenancy, monkeypatch):
    seen: list[str] = []

    def fake_fetch(symbol, **_k):
        seen.append(symbol)
        return {"available": False, "reason": "test"}

    monkeypatch.setattr(exday, "fetch", fake_fetch)
    pe._collect_fund_structure_block(tenancy["db"], None, "2026-10-29")
    assert tenancy["syms"]["a_pos"] not in seen
    assert tenancy["syms"]["b_pos"] not in seen
    assert tenancy["syms"]["a_watch"] not in seen
    assert tenancy["syms"]["b_watch"] not in seen
