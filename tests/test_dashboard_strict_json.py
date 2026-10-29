"""看板/决策 JSON 严格性 + 账户作用域回归(2026-10-29 三审 R2)。

修前实测:

1. `/api/dashboard/overview` 的持仓统计只按 `Position.user_id` 过滤 → 共享账户/他人
   私有账户里错位的行被算进"我的持仓数/投入成本"; 可用资金直接 `SUM(accounts)` 全表
   → 把别人私有账户的钱算进我的总资产。
2. 历史脏成本价是 NaN/Inf 时, `float(nan)` 能过 `or 0.0`, 一路污染 `invested_cost`/
   `by_market`, Python 的 json 会把它写成 `NaN` / `Infinity` **字面量** —— 那不是
   合法 JSON, 前端 `JSON.parse` 直接抛, 整个看板白屏。
3. `/api/decision/{symbol}` 把 `position.cost_price` 原样塞进响应, 同样会带出 NaN。

本文件锁定: 响应必须是严格 JSON(可用 `json.loads(..., parse_constant=raise)` 校验),
脏成本算"未定价"并单独计数(不拿 0 冒充), 统计只数自己可见的账户/持仓。
"""

from __future__ import annotations

import json
import math
import uuid

import pytest
from fastapi.testclient import TestClient

from src.db.models import Account, Position, Stock, User
from src.web.database import SessionLocal, init_db

PREFIX = "dsh"


def _strict_json(text: str):
    """严格 JSON 解析: 遇到 NaN/Infinity 字面量直接失败(浏览器 JSON.parse 同口径)。"""

    def _bad(const):
        raise AssertionError(f"响应里出现非严格 JSON 字面量: {const}")

    return json.loads(text, parse_constant=_bad)


@pytest.fixture(scope="module", autouse=True)
def _db_ready():
    init_db()


@pytest.fixture()
def client():
    from src.web.app import app

    return TestClient(app)


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

        acc_a = Account(name=f"{PREFIX}-A-{tag}", available_funds=1000.0, enabled=True, user_id=a.id)
        acc_b = Account(name=f"{PREFIX}-B-{tag}", available_funds=999999.0, enabled=True, user_id=b.id)
        db.add_all([acc_a, acc_b])
        db.commit()
        created["accounts"] = [acc_a.id, acc_b.id]

        def mk(acc, uid, sym, cost, qty):
            st = Stock(symbol=sym, name=sym, market="CN", user_id=None)
            db.add(st)
            db.flush()
            # 历史脏数据: cost_price 可能是 Inf(生产 PG 里还可能是 NaN)
            pos = Position(account_id=acc.id, stock_id=st.id, cost_price=cost, quantity=qty,
                           user_id=uid)
            db.add(pos)
            db.commit()
            created["stocks"].append(st.id)
            created["positions"].append(pos.id)
            return pos

        from src.web.api.auth import create_token

        tok_a, _ = create_token(a)

        yield {
            "db": db,
            "a": a,
            "b": b,
            "acc_a": acc_a,
            "acc_b": acc_b,
            "sym_good": mk(acc_a, a.id, f"DG{tag}", 10.0, 100),
            # 注: SQLite 的 Python 驱动会把 NaN 绑定成 NULL(撞 NOT NULL 约束), 所以这里
            # 用 ±inf 落库(同样是非有限数, 走同一条 finite 校验); NaN 的具体路径由
            # `test_decision_personal_context_never_emits_nan`(纯对象) 与 PG 临时库验证覆盖。
            "sym_nan": mk(acc_a, a.id, f"DN{tag}", float("inf"), 100),
            "sym_inf": mk(acc_a, a.id, f"DI{tag}", float("-inf"), 50),
            "b_pos": mk(acc_b, b.id, f"DB{tag}", 10.0, 100),
            # 错位行: A 的账户里混入 B 的持仓(必须不算进 A 的统计)
            "misplaced": mk(acc_a, b.id, f"DM{tag}", 10.0, 100),
            "token_a": tok_a,
            "created": created,
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


def test_dashboard_overview_is_strict_json_and_scoped(client, tenancy):
    h = {"Authorization": f"Bearer {tenancy['token_a']}"}
    r = client.get("/api/dashboard/overview", headers=h)
    assert r.status_code == 200, r.text
    payload = _strict_json(r.text)
    data = payload["data"]

    kpis = data["kpis"]
    # A 可见: 自己的 3 条(good/nan/inf); B 私有账户那条与错位行都不算
    assert kpis["positions_count"] == 3, kpis
    # 可用资金只算 A 自己的账户(B 的 999999 不能进我的总资产)
    assert kpis["available_funds"] == pytest.approx(1000.0, rel=1e-6)
    # 只有 good 那条有价: 10.0 * 100 = 1000
    assert kpis["invested_cost"] == pytest.approx(1000.0, rel=1e-6)
    # NaN/Inf 两条不算进成本, 但要如实计数
    assert kpis["unpriced_positions"] == 2, kpis
    for bucket in data["portfolio"]["by_market"]:
        assert math.isfinite(bucket["invested_cost"])


def test_dashboard_overview_without_dirty_costs_still_finite(client, tenancy):
    """即便没有脏数据, 所有数值字段也必须是有限数(防止回归)。"""
    h = {"Authorization": f"Bearer {tenancy['token_a']}"}
    d = _strict_json(client.get("/api/dashboard/overview", headers=h).text)["data"]
    for key in ("available_funds", "invested_cost", "total_assets_estimate"):
        assert math.isfinite(d["kpis"][key]), key


def test_decision_personal_context_never_emits_nan(tenancy, monkeypatch):
    """决策上下文里的脏成本必须变成 null + cost_unknown, 不能进 JSON。"""
    import src.core.portfolio_scope as ps
    from src.web.api import decision as decision_api
    from src.web.models import Stock as WebStock

    class _Q:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return list(self._rows)

        def first(self):
            return self._rows[0] if self._rows else None

    class _DB:
        def __init__(self, stocks, positions):
            self._stocks = stocks
            self._positions = positions

        def query(self, *models, **_k):
            name = models[0].__name__
            if name == "Stock":
                return _Q(self._stocks)
            if name == "Position":
                return _Q(self._positions)
            return _Q([])

    row = tenancy["db"].query(Stock).filter(Stock.id == tenancy["sym_nan"].stock_id).first()
    stock = type("S", (), {"id": row.id, "symbol": row.symbol, "market": "CN"})()
    pos = type(
        "P",
        (),
        {
            "stock_id": row.id,
            "cost_price": float("nan"),
            "quantity": 100,
            "trading_style": "swing",
        },
    )()

    monkeypatch.setattr(ps, "user_scope_positions", lambda db, uid: _Q([pos]))
    monkeypatch.setattr(decision_api, "scoped", lambda q, user: _Q([stock]))
    assert WebStock is not None  # 确认走的是同一 ORM 命名空间

    ctx = decision_api._build_user_context(_DB([stock], [pos]), tenancy["a"], row.symbol, "CN")
    assert ctx is not None
    assert ctx["holds"] is True
    assert ctx["cost_price"] is None, "NaN 成本必须归零为 null"
    assert ctx["cost_unknown"] is True
    json.dumps(ctx, allow_nan=False)


def _is_postgres(db) -> bool:
    try:
        return db.get_bind().dialect.name == "postgresql"
    except Exception:  # noqa: BLE001
        return False


def test_dashboard_overview_with_nan_cost_on_postgres(client, tenancy):
    """PG 的 double precision 允许 NaN(生产真实的脏数据形态) —— 必须算"未定价"且 JSON 严格。

    SQLite 驱动会把 NaN 绑成 NULL(撞 NOT NULL), 所以这条只在 PG 临时库跑; SQLite 环境
    显式 skip, 不假装覆盖。
    """
    db = tenancy["db"]
    if not _is_postgres(db):
        pytest.skip("SQLite 无法存 NaN(驱动绑定为 NULL), NaN 路径由 PG 临时库验证")

    from sqlalchemy import text

    pos_id = tenancy["sym_nan"].id
    db.execute(text("UPDATE positions SET cost_price = 'NaN'::float8 WHERE id = :i"), {"i": pos_id})
    db.commit()

    h = {"Authorization": f"Bearer {tenancy['token_a']}"}
    r = client.get("/api/dashboard/overview", headers=h)
    assert r.status_code == 200, r.text
    data = _strict_json(r.text)["data"]
    assert math.isfinite(data["kpis"]["invested_cost"])
    assert data["kpis"]["invested_cost"] == pytest.approx(1000.0, rel=1e-6)
    assert data["kpis"]["unpriced_positions"] >= 1
