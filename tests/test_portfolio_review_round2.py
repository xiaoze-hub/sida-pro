"""持仓越权复审第二轮(2026-10-29): create 越权 / 通知渠道错发 / 非有限数 500 / 上下文未知 / 同族 IDOR。

本文件锁定四类**实测**残留(修复前逐条复现, 修复后逐条断言):

1. `POST /api/positions` 只校验账户"可读":
   member 能往共享账户(user_id=NULL)里建仓(违背"共享仅 owner 写");
   `existing` / `max_order` 不带租户过滤 —— 他人(含共享账户里别人建的)同票持仓会让
   请求得到带股票名的 400, 且把他人 sort_order 当成自己的下一位;
   并发建同一 (account_id, stock_id) 撞 `uq_account_stock` 时直接 500。
2. `trigger_agent_for_stock` 解析通知渠道时不传 user_id: member 手动触发会落回
   agent/stock_agent 级(owner 的)渠道 → member 的分析发进 owner 私有渠道。
3. Pydantic `allow_inf_nan=False` 的 422 响应会把 `NaN`/`Infinity` 原样回显,
   Starlette `JSONResponse(allow_nan=False)` 序列化失败 → **实际 HTTP 500**。
4. 无会话 + 标的无归属: 组合上下文按共享桶 fail-closed 后只剩空, 渲染层却断言
   "用户不持有" —— 把"不知道"冒充成事实。
5. 同族 Position IDOR(`decision._build_user_context` / `intraday_monitor.load_defense_lines`):
   只按 `Position.user_id` 过滤, 他人私有账户里 user_id=NULL 的持仓照样被读到。

口径见 `src/core/portfolio_scope.py`: 账户可见 AND 持仓可见; 共享仅 owner 写;
他人私有统一 404; 没会话 → 共享桶, 不猜 owner。
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from src.web.database import SessionLocal, init_db
from src.web.models import Account, AgentConfig, NotifyChannel, Position, Stock, User

PREFIX = "rv"


@pytest.fixture(scope="module", autouse=True)
def _db_ready():
    init_db()


@pytest.fixture()
def client():
    from src.web.app import app

    return TestClient(app, raise_server_exceptions=False)


def _mk_user(db, role: str) -> User:
    user = User(
        id=str(uuid.uuid4()),
        username=f"{PREFIX}_{role}_{uuid.uuid4().hex[:10]}",
        password_hash="not-a-real-hash",
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


def _mk_stock(db, symbol: str) -> Stock:
    stock = Stock(symbol=symbol, name=f"{symbol}-name", market="CN", user_id=None)
    db.add(stock)
    db.commit()
    return stock


def _mk_position(db, *, acc, stock, user_id, cost=10.0, qty=100, sort=None) -> Position:
    pos = Position(
        account_id=acc.id,
        stock_id=stock.id,
        cost_price=cost,
        quantity=qty,
        user_id=user_id,
        sort_order=sort,
    )
    db.add(pos)
    db.commit()
    return pos


@pytest.fixture()
def tenancy(client):
    from src.web.api.auth import create_token

    db = SessionLocal()
    tag = uuid.uuid4().hex[:6].upper()  # 符号大写: 生产 ticker 是大写, 用例也不该靠大小写碰巧对上
    created = {"users": [], "accounts": [], "positions": [], "stocks": [], "channels": []}
    try:
        a = _mk_user(db, "member")
        b = _mk_user(db, "member")
        o = _mk_user(db, "owner")
        created["users"] = [a.id, b.id, o.id]

        acc_a = _mk_account(db, user_id=a.id, name=f"{PREFIX}-A-{tag}")
        acc_b = _mk_account(db, user_id=b.id, name=f"{PREFIX}-B-{tag}")
        acc_shared = _mk_account(db, user_id=None, name=f"{PREFIX}-SHARED-{tag}")
        created["accounts"] = [acc_a.id, acc_b.id, acc_shared.id]

        st1 = _mk_stock(db, f"RV{tag}1")
        st2 = _mk_stock(db, f"RV{tag}2")
        st3 = _mk_stock(db, f"RV{tag}3")
        st4 = _mk_stock(db, f"RV{tag}4")
        created["stocks"] = [s.id for s in (st1, st2, st3, st4)]

        pos = {
            # B 私有账户里 user_id=NULL 的持仓: 修复前 A 可在 defense/decision 读到
            "b_null": _mk_position(db, acc=acc_b, stock=st1, user_id=None, cost=6.0),
            # 共享账户里的历史 NULL 持仓: 所有人可读
            "shared_null": _mk_position(db, acc=acc_shared, stock=st2, user_id=None, cost=7.0),
            # 共享账户里 B 建的持仓: 共享桶里的他人私有行
            "shared_b": _mk_position(db, acc=acc_shared, stock=st3, user_id=b.id, cost=8.0),
            # A 私有账户里混入的 B 持仓(数据错位), sort_order 故意很大
            "a_foreign": _mk_position(
                db, acc=acc_a, stock=st4, user_id=b.id, cost=9.0, sort=99
            ),
        }
        created["positions"] = [p.id for p in pos.values()]

        # 通知渠道: owner 私有 + 全局共享; agent 级默认指向 owner 私有渠道
        ch_owner = NotifyChannel(
            user_id=o.id, name=f"{PREFIX}-OWNER-CH-{tag}", type="webhook",
            config={"url": "http://127.0.0.1:9/owner"}, enabled=True,
        )
        ch_shared = NotifyChannel(
            user_id=None, name=f"{PREFIX}-SHARED-CH-{tag}", type="webhook",
            config={"url": "http://127.0.0.1:9/shared"}, enabled=True,
        )
        db.add_all([ch_owner, ch_shared])
        db.commit()
        created["channels"] = [ch_owner.id, ch_shared.id]

        agent_name = f"{PREFIX}-agent-{tag}"
        db.add(
            AgentConfig(
                name=agent_name,
                display_name=agent_name,
                notify_channel_ids=[ch_owner.id],
            )
        )
        db.commit()

        tokens = {}
        for name, user in (("a", a), ("b", b), ("o", o)):
            token, _exp = create_token(user)
            tokens[name] = token

        yield {
            "tag": tag,
            "users": {"a": a, "b": b, "o": o},
            "accounts": {"a": acc_a, "b": acc_b, "shared": acc_shared},
            "stocks": {"s1": st1, "s2": st2, "s3": st3, "s4": st4},
            "pos": pos,
            "tokens": tokens,
            "channels": {"owner": ch_owner, "shared": ch_shared},
            "agent_name": agent_name,
        }
    finally:
        db.rollback()
        db.query(Position).filter(Position.id.in_(created["positions"])).delete(
            synchronize_session=False
        )
        db.query(NotifyChannel).filter(NotifyChannel.id.in_(created["channels"])).delete(
            synchronize_session=False
        )
        db.query(AgentConfig).filter(AgentConfig.name.like(f"{PREFIX}-agent-%")).delete(
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


def _err(resp) -> str:
    try:
        return str(resp.json().get("message") or "")
    except Exception:  # noqa: BLE001
        return resp.text


# ================================================================ 1. create_position


def test_member_cannot_create_position_in_shared_account(client, tenancy):
    """共享账户(user_id=NULL)对 member 只读: 建仓必须 403, 且库里不能多行。"""
    acc = tenancy["accounts"]["shared"]
    stock = tenancy["stocks"]["s3"]
    r = client.post(
        "/api/positions",
        json={"account_id": acc.id, "stock_id": stock.id, "cost_price": 11.0, "quantity": 100},
        headers=_hdr(tenancy["tokens"], "a"),
    )
    assert r.status_code == 403, r.text

    db = SessionLocal()
    try:
        assert (
            db.query(Position)
            .filter(Position.account_id == acc.id, Position.stock_id == stock.id)
            .count()
            == 1  # 只有 B 原来那行
        )
    finally:
        db.close()


def test_owner_can_create_position_in_shared_account(client, tenancy):
    """共享账户 owner 可写: 空标的建仓成功。"""
    acc = tenancy["accounts"]["shared"]
    stock = tenancy["stocks"]["s1"]
    r = client.post(
        "/api/positions",
        json={"account_id": acc.id, "stock_id": stock.id, "cost_price": 12.0, "quantity": 200},
        headers=_hdr(tenancy["tokens"], "o"),
    )
    assert r.status_code == 200, r.text
    new_id = r.json()["data"]["id"]

    db = SessionLocal()
    try:
        row = db.get(Position, new_id)
        assert row is not None and row.user_id == tenancy["users"]["o"].id
        db.delete(row)
        db.commit()
    finally:
        db.close()


def test_member_can_create_position_in_own_account(client, tenancy):
    acc = tenancy["accounts"]["a"]
    stock = tenancy["stocks"]["s2"]
    r = client.post(
        "/api/positions",
        json={"account_id": acc.id, "stock_id": stock.id, "cost_price": 13.0, "quantity": 300},
        headers=_hdr(tenancy["tokens"], "a"),
    )
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        pid = r.json()["data"]["id"]
        db.query(Position).filter(Position.id == pid).delete()
        db.commit()
    finally:
        db.close()


def test_create_position_in_foreign_private_account_is_404(client, tenancy):
    acc = tenancy["accounts"]["b"]
    stock = tenancy["stocks"]["s4"]
    r = client.post(
        "/api/positions",
        json={"account_id": acc.id, "stock_id": stock.id, "cost_price": 1.0, "quantity": 1},
        headers=_hdr(tenancy["tokens"], "a"),
    )
    assert r.status_code == 404, r.text


def test_create_position_conflict_does_not_leak_hidden_sibling(client, tenancy):
    """共享账户里 B 已持有该票: owner 再建仓撞唯一约束 → 409 通用文案, 不报出别人的票名。"""
    acc = tenancy["accounts"]["shared"]
    stock = tenancy["stocks"]["s3"]  # shared_b(B 的持仓) 用的就是它
    r = client.post(
        "/api/positions",
        json={"account_id": acc.id, "stock_id": stock.id, "cost_price": 14.0, "quantity": 400},
        headers=_hdr(tenancy["tokens"], "o"),
    )
    assert r.status_code == 409, r.text
    msg = _err(r)
    assert stock.name not in msg, f"冲突文案泄漏了他人持仓标的: {msg}"
    assert "已有" not in msg, f"冲突文案暗示了他人持仓: {msg}"
    assert str(tenancy["users"]["b"].id) not in msg

    db = SessionLocal()
    try:
        assert (
            db.query(Position)
            .filter(Position.account_id == acc.id, Position.stock_id == stock.id)
            .count()
            == 1
        )
    finally:
        db.close()


def test_create_position_sort_order_ignores_hidden_rows(client, tenancy):
    """他人(不可见)行的 sort_order 不得参与我的下一位计算。"""
    acc = tenancy["accounts"]["a"]  # 里面混入 B 的 sort_order=99
    stock = tenancy["stocks"]["s1"]
    r = client.post(
        "/api/positions",
        json={"account_id": acc.id, "stock_id": stock.id, "cost_price": 15.0, "quantity": 500},
        headers=_hdr(tenancy["tokens"], "a"),
    )
    assert r.status_code == 200, r.text
    new_id = r.json()["data"]["id"]
    assert r.json()["data"]["sort_order"] == 1, "不可见行(99)不得影响我这一行的序号"
    db = SessionLocal()
    try:
        db.query(Position).filter(Position.id == new_id).delete()
        db.commit()
    finally:
        db.close()


def test_non_finite_create_position_returns_safe_422(client, tenancy):
    """NaN/Inf 入参: 422 且响应体是合法 JSON(修复前 500), 库里不落行。"""
    acc = tenancy["accounts"]["a"]
    stock = tenancy["stocks"]["s2"]
    hdr = {**_hdr(tenancy["tokens"], "a"), "Content-Type": "application/json"}
    raws = [
        '{"account_id": %d, "stock_id": %d, "cost_price": NaN, "quantity": 1}'
        % (acc.id, stock.id),
        '{"account_id": %d, "stock_id": %d, "cost_price": 1.0, "quantity": 1, '
        '"invested_amount": Infinity}' % (acc.id, stock.id),
    ]
    for raw in raws:
        r = client.post("/api/positions", content=raw, headers=hdr)
        assert r.status_code == 422, r.text
        # 必须是合法 JSON: 严格解析器不接受裸 NaN/Infinity
        parsed = json.loads(
            r.text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(f"裸常量 {c}"))
        )
        assert parsed["success"] is False
    db = SessionLocal()
    try:
        assert (
            db.query(Position)
            .filter(Position.account_id == acc.id, Position.stock_id == stock.id)
            .count()
            == 0
        )
    finally:
        db.close()


def test_non_finite_defense_update_is_rejected_and_not_persisted(client, tenancy):
    """防守线 NaN/Inf: 422(不是 500), 库里仍是 NULL。"""
    p = tenancy["pos"]["shared_null"]
    hdr = {**_hdr(tenancy["tokens"], "o"), "Content-Type": "application/json"}
    for raw in ('{"defense_price": NaN}', '{"defense_price": Infinity}', '{"defense_price": -Infinity}'):
        r = client.put(f"/api/positions/{p.id}/defense", content=raw, headers=hdr)
        assert r.status_code == 422, r.text
        json.loads(r.text)
    db = SessionLocal()
    try:
        assert db.get(Position, p.id).defense_price is None
    finally:
        db.close()


def test_non_finite_account_funds_response_is_serializable(client, tenancy):
    """跨模型回归: 账户可用资金 NaN 同样是 422 + 合法 JSON。"""
    hdr = {**_hdr(tenancy["tokens"], "a"), "Content-Type": "application/json"}
    r = client.post('/api/accounts', content='{"name": "rv-nan", "available_funds": NaN}', headers=hdr)
    assert r.status_code == 422, r.text
    parsed = json.loads(r.text)
    assert parsed["success"] is False
    db = SessionLocal()
    try:
        assert db.query(Account).filter(Account.name == "rv-nan").count() == 0
    finally:
        db.close()


def test_concurrent_non_finite_posts_never_500(client, tenancy):
    """并发 NaN 请求也不得出现 500 / 非法 JSON。"""
    import concurrent.futures

    from src.web.app import app

    acc = tenancy["accounts"]["a"]
    stock = tenancy["stocks"]["s2"]
    hdr = {**_hdr(tenancy["tokens"], "a"), "Content-Type": "application/json"}
    raw = '{"account_id": %d, "stock_id": %d, "cost_price": NaN, "quantity": 1}' % (acc.id, stock.id)

    def _one(_i):
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post("/api/positions", content=raw, headers=hdr)
        return r.status_code, r.text

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        out = list(ex.map(_one, range(8)))
    for code, text in out:
        assert code == 422, (code, text)
        json.loads(text)


def test_concurrent_create_same_stock_is_race_safe(client, tenancy):
    """真并发建同一 (account, stock): 恰好一胜, 其余通用 409, 无 500, 库里只有一行。

    只在真实 PG 上跑竞态: SQLite 写锁会把并发变成 "database is locked",
    证明不了唯一约束 + 回滚那条路径。隔离库跑法见模块头注/CHANGELOG-less 说明:
        SIDA_DB_URL=postgresql+psycopg2://.../<隔离库> python -m pytest tests/test_portfolio_review_round2.py
    """
    import concurrent.futures

    from src.db.dialect import is_postgres
    from src.web.app import app

    if not is_postgres():
        pytest.skip("竞态路径需真实 PG 隔离库")

    acc = tenancy["accounts"]["a"]
    stock = tenancy["stocks"]["s2"]
    hdr = _hdr(tenancy["tokens"], "a")
    payload = {
        "account_id": acc.id,
        "stock_id": stock.id,
        "cost_price": 21.0,
        "quantity": 600,
    }

    def _one(_i):
        c = TestClient(app, raise_server_exceptions=False)
        r = c.post("/api/positions", json=payload, headers=hdr)
        return r.status_code, r.text

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
            out = list(ex.map(_one, range(6)))
        codes = [c for c, _ in out]
        assert codes.count(200) == 1, out
        # 同一个人自己并发: 后到的请求可能先看到自己那行(可见 → 400 带名字, 这是他的
        # 自有数据), 也可能在窗口内直接撞唯一约束(→ 通用 409)。两者都不许出现 500,
        # 也不许出现别人的名字。
        assert set(codes) <= {200, 400, 409}, out
        for code, text in out:
            if code == 409:
                assert "已存在或正在创建" in text
                assert stock.name not in text and acc.name not in text
            if code == 400:
                assert "请编辑现有持仓" in text, text
                assert tenancy["users"]["b"].id not in text

        db = SessionLocal()
        try:
            assert (
                db.query(Position)
                .filter(Position.account_id == acc.id, Position.stock_id == stock.id)
                .count()
                == 1
            ), "并发冲突必须回滚干净, 不能留下半条记录"
            # 回滚后的会话必须还能用(没有 aborted transaction 残留)
            assert db.query(Position).filter(Position.account_id == acc.id).count() >= 1
        finally:
            db.rollback()
            db.query(Position).filter(
                Position.account_id == acc.id, Position.stock_id == stock.id
            ).delete(synchronize_session=False)
            db.commit()
            db.close()
    finally:
        c = TestClient(app)
        c.get("/api/positions", headers=hdr)  # 后续请求不受影响


def test_unique_conflict_detection_covers_both_dialects():
    """唯一冲突判定要同时认 PG(SQLSTATE 23505) 与 SQLite(UNIQUE constraint failed)。"""
    from sqlalchemy.exc import IntegrityError

    from src.web.api.accounts import _is_unique_conflict

    class _Orig:
        def __init__(self, text, pgcode=None):
            self._text = text
            self.pgcode = pgcode

        def __str__(self):
            return self._text

    pg = IntegrityError("insert", {}, _Orig("duplicate key value violates unique constraint", "23505"))
    sqlite = IntegrityError("insert", {}, _Orig("UNIQUE constraint failed: positions.account_id, positions.stock_id"))
    fk = IntegrityError("insert", {}, _Orig("insert violates foreign key constraint", "23503"))

    assert _is_unique_conflict(pg) is True
    assert _is_unique_conflict(sqlite) is True
    assert _is_unique_conflict(fk) is False


# ================================================================ 2. 通知渠道作用域


def test_resolve_notify_channels_explicit_none_is_shared_only(tenancy, monkeypatch):
    """显式 user_id=None = 没有会话 → 只取共享渠道, 不许落回 agent 级(owner)渠道。"""
    import src.bootstrap.runtime as rt

    channels = rt.resolve_notify_channels(tenancy["agent_name"], user_id=None)
    ids = {c.id for c in channels}
    assert tenancy["channels"]["owner"].id not in ids, "无会话不得取 owner 的 agent 级渠道"
    assert tenancy["channels"]["shared"].id in ids


def test_resolve_notify_channels_user_scope_excludes_owner_private(tenancy):
    import src.bootstrap.runtime as rt

    channels = rt.resolve_notify_channels(
        tenancy["agent_name"], user_id=tenancy["users"]["a"].id
    )
    ids = {c.id for c in channels}
    assert tenancy["channels"]["owner"].id not in ids
    assert tenancy["channels"]["shared"].id in ids


def _spy_trigger(monkeypatch, captured: dict):
    import src.bootstrap.runtime as rt

    def _spy(agent_name, stock_agent_id=None, user_id=rt._UNSET):
        captured["call"] = {"agent_name": agent_name, "user_id": user_id}
        raise RuntimeError("stop-after-channels")

    monkeypatch.setattr(rt, "resolve_notify_channels", _spy)
    return rt


def test_trigger_agent_for_stock_passes_user_id_to_channels(tenancy, monkeypatch):
    """member 手动触发: 通知渠道必须按触发用户收敛(修复前发进 owner 私有渠道)。"""
    import asyncio
    from types import SimpleNamespace

    captured: dict = {}
    rt = _spy_trigger(monkeypatch, captured)
    agent_name = next(iter(rt.AGENT_REGISTRY))
    stock = SimpleNamespace(id=987654, symbol="RVSPY", name="spy", market="CN", user_id=None)

    with pytest.raises(RuntimeError):
        asyncio.run(
            rt.trigger_agent_for_stock(agent_name, stock, user_id=tenancy["users"]["a"].id)
        )
    assert captured["call"]["user_id"] == tenancy["users"]["a"].id


def test_trigger_agent_for_stock_without_session_scopes_to_stock_owner(tenancy, monkeypatch):
    """系统调度: 按标的显式归属收敛(与组合上下文同一租户), 不落回 agent 全局渠道。"""
    import asyncio
    from types import SimpleNamespace

    captured: dict = {}
    rt = _spy_trigger(monkeypatch, captured)
    agent_name = next(iter(rt.AGENT_REGISTRY))
    stock = SimpleNamespace(
        id=987655, symbol="RVSPY2", name="spy", market="CN", user_id=tenancy["users"]["b"].id
    )

    with pytest.raises(RuntimeError):
        asyncio.run(rt.trigger_agent_for_stock(agent_name, stock))
    assert captured["call"]["user_id"] == tenancy["users"]["b"].id


def test_trigger_agent_for_stock_without_session_or_owner_fails_closed(tenancy, monkeypatch):
    """无会话 + 标的无归属: 显式 None(仅共享渠道), 不扩大收件范围。"""
    import asyncio
    from types import SimpleNamespace

    captured: dict = {}
    rt = _spy_trigger(monkeypatch, captured)
    agent_name = next(iter(rt.AGENT_REGISTRY))
    stock = SimpleNamespace(id=987656, symbol="RVSPY3", name="spy", market="CN", user_id=None)

    with pytest.raises(RuntimeError):
        asyncio.run(rt.trigger_agent_for_stock(agent_name, stock))
    assert captured["call"]["user_id"] is None, "缺会话必须显式 None(共享桶), 不能是 _UNSET(legacy 全局)"


# ================================================================ 3. 组合上下文未知


def test_load_portfolio_for_stock_without_session_and_owner_marks_unknown(tenancy, monkeypatch):
    import src.bootstrap.runtime as rt
    from sqlalchemy.orm import sessionmaker

    TestingSession = sessionmaker(bind=SessionLocal().get_bind(), expire_on_commit=False)
    monkeypatch.setattr(rt, "SessionLocal", TestingSession)

    stock = tenancy["stocks"]["s1"]  # stocks.user_id = NULL
    info = rt.load_portfolio_for_stock(stock.id)
    assert info.scope_note, "无会话且标的无归属时必须显式标注上下文未知"
    assert "用户会话" in info.scope_note or "归属" in info.scope_note
    # 仍然只给共享桶, 不猜 owner
    for acc in info.accounts:
        assert acc.id != tenancy["accounts"]["b"].id
        assert acc.id != tenancy["accounts"]["a"].id


def test_portfolio_context_does_not_assert_absence_when_scope_unknown(tenancy, monkeypatch):
    """渲染层: 上下文未知时不得输出\"用户不持有\"。"""
    import src.bootstrap.runtime as rt
    from src.agents.tradingagents.portfolio_context import build_portfolio_context
    from sqlalchemy.orm import sessionmaker

    TestingSession = sessionmaker(bind=SessionLocal().get_bind(), expire_on_commit=False)
    monkeypatch.setattr(rt, "SessionLocal", TestingSession)

    stock = tenancy["stocks"]["s1"]
    info = rt.load_portfolio_for_stock(stock.id)
    text = build_portfolio_context(info, stock.symbol)
    assert "does NOT currently hold" not in text
    assert "未知" in text or "unknown" in text.lower()


# ================================================================ 4. 同族 IDOR


def test_load_defense_lines_hides_null_position_in_foreign_private_account(tenancy):
    """B 私有账户里 user_id=NULL 的防守线: A 读不到(B 自己读得到)。"""
    p = tenancy["pos"]["b_null"]
    db = SessionLocal()
    try:
        row = db.get(Position, p.id)
        row.defense_price = 5.5
        db.commit()
    finally:
        db.close()

    from src.agents.intraday_monitor import load_defense_lines

    a_rows = load_defense_lines(p.stock.symbol, user_id=tenancy["users"]["a"].id)
    assert all(row["position_id"] != p.id for row in a_rows), "A 不得读到 B 私有账户里的持仓"

    b_rows = load_defense_lines(p.stock.symbol, user_id=tenancy["users"]["b"].id)
    assert any(row["position_id"] == p.id for row in b_rows)

    # 无会话(系统): 共享桶 —— 仍不得包含 B 私有账户的行
    no_user_rows = load_defense_lines(p.stock.symbol)
    assert all(row["position_id"] != p.id for row in no_user_rows)


def test_load_defense_lines_keeps_shared_account_rows(tenancy):
    p = tenancy["pos"]["shared_null"]
    db = SessionLocal()
    try:
        row = db.get(Position, p.id)
        row.defense_price = 6.6
        db.commit()
    finally:
        db.close()

    from src.agents.intraday_monitor import load_defense_lines

    for uid in (tenancy["users"]["a"].id, tenancy["users"]["b"].id, None):
        rows = load_defense_lines(p.stock.symbol, user_id=uid)
        assert any(row["position_id"] == p.id for row in rows), f"共享持仓必须对 uid={uid} 可见"


def test_decision_user_context_ignores_null_position_in_foreign_account(tenancy):
    """决策个性化: A 不得因为 B 私有账户里的 NULL 持仓而显示"已持仓"。"""
    from src.web.api.decision import _build_user_context

    p = tenancy["pos"]["b_null"]
    db = SessionLocal()
    try:
        ctx_a = _build_user_context(db, tenancy["users"]["a"], p.stock.symbol, "CN")
        ctx_b = _build_user_context(db, tenancy["users"]["b"], p.stock.symbol, "CN")
    finally:
        db.close()
    assert ctx_a["holds"] is False, "A 不得读到 B 私有账户里的持仓"
    assert ctx_b["holds"] is True


def test_owner_cost_anchor_ignores_null_position_in_foreign_account(tenancy):
    """聊天工具的成本锚定(scenario_compose.owner_cost)同样按账户+持仓双口径收敛。"""
    from src.core.scenario_compose import owner_cost

    p = tenancy["pos"]["b_null"]
    db = SessionLocal()
    try:
        cost_a, _stop_a = owner_cost(db, tenancy["users"]["a"], p.stock.symbol)
        cost_b, _stop_b = owner_cost(db, tenancy["users"]["b"], p.stock.symbol)
        cost_anon, _ = owner_cost(db, None, p.stock.symbol)
    finally:
        db.close()
    assert cost_a is None, "A 不得把 B 私有账户里的持仓成本当成自己的"
    assert cost_b == pytest.approx(6.0)
    assert cost_anon is None, "无用户时 fail-closed, 不得取任意持仓成本"
