"""价格提醒配额 + AI 动作副作用披露(2026-11-07)。

钉住四件事:

1. **两条写入路径都不能绕过配额**: `POST /api/price-alerts` 与
   `copilot_actions.apply(kind=alert)` 都受 `check_alert_quota` 约束;
   AI 动作顺带建的自选同样受 `check_watchlist_quota` 约束。
2. **确认前不写业务表**: 提议只落 pending; 配额被拒时动作必须仍是 pending,
   不留下"已被认领却没执行"的半截状态, 也不留半条规则/自选。
3. **副作用如实披露**: 提醒动作会顺带把标的不存在的自选建出来 —— diff、
   执行结果文案、工具提示词三处口径一致; 撤销时两样一起退回。
4. **跨账户隔离**: 配额只数本人规则; 拿别人的 action_id 不能执行;
   建提醒不得复用/改写他人或别的市场的自选行。
"""

from __future__ import annotations

import asyncio
import os

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import free_tier
from src.db.models import Base, CopilotAction, PriceAlertRule, Stock, User
from src.harness import actions as act
from src.web.api.price_alerts import (
    AlertConditionGroup,
    AlertConditionItem,
    PriceAlertCreate,
    create_alert_rule,
)


def _test_engine():
    """默认定向回归跑 SQLite 内存库; 设 `ALERT_TEST_PG_URL` 时跑**隔离的 PostgreSQL 库**。

    2026-11-07 复审要求: 「SQLite 绿」不能当「PG 绿」交付。级联删除、JSON 列、
    行锁语义在两种方言下不同(尤其 `ondelete=CASCADE` 在 SQLite 默认不生效),
    所以同一组用例必须能在真 PG 上再跑一遍, 且库/角色是**专建专用**的, 不碰任何现有库。
    """
    url = os.environ.get("ALERT_TEST_PG_URL")
    if url:
        return create_engine(url, future=True), True
    return (
        create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool),
        False,
    )


@pytest.fixture()
def db():
    eng, is_pg = _test_engine()
    Base.metadata.drop_all(eng) if is_pg else None
    Base.metadata.create_all(eng)
    free_tier.invalidate_cache()
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()
        free_tier.invalidate_cache()


def _user(db, uid: str, role: str = "member", username: str | None = None) -> User:
    row = User(id=uid, username=username or f"u-{uid}", password_hash="x", role=role)
    db.add(row)
    db.commit()
    return row


def _stock(db, uid: str, symbol: str, market: str = "CN") -> Stock:
    row = Stock(user_id=uid, symbol=symbol, name=f"股{symbol}", market=market)
    db.add(row)
    db.commit()
    return row


def _rule(db, uid: str, stock: Stock, price: float = 10.0) -> PriceAlertRule:
    row = PriceAlertRule(
        user_id=uid,
        stock_id=stock.id,
        name=f"{stock.symbol} 提醒",
        enabled=True,
        condition_group={
            "op": "and",
            "items": [{"type": "price", "op": ">=", "value": price}],
        },
    )
    db.add(row)
    db.commit()
    return row


def _fill_rules(db, uid: str, n: int) -> list[PriceAlertRule]:
    """给 uid 塞满 n 条规则(每条的标的自选各自独立)。"""
    return [_rule(db, uid, _stock(db, uid, f"60{i:04d}")) for i in range(n)]


def _body(stock: Stock) -> PriceAlertCreate:
    return PriceAlertCreate(
        stock_id=stock.id,
        condition_group=AlertConditionGroup(
            items=[AlertConditionItem(type="price", op=">=", value=10)]
        ),
    )


def _pending(db, action_id: int) -> CopilotAction:
    db.expire_all()
    return db.query(CopilotAction).filter(CopilotAction.id == action_id).one()


# ── 直接 API 路径 ─────────────────────────────────────────────────────


def test_api_第4条提醒被配额拦截且不落库(db):
    user = _user(db, "u1")
    stocks = [_stock(db, "u1", f"60{i:04d}") for i in range(4)]
    for st in stocks[:3]:
        _rule(db, "u1", st)

    with pytest.raises(HTTPException) as ei:
        create_alert_rule(_body(stocks[3]), db=db, user=user)

    assert ei.value.status_code == 403
    assert "预警上限" in str(ei.value.detail)
    assert db.query(PriceAlertRule).count() == 3, "被拒的创建不能落库"


def test_api_配额按账号隔离_他人塞满不影响本人(db):
    _user(db, "u1")
    _user(db, "u2")
    _fill_rules(db, "u1", 3)
    mine = _stock(db, "u2", "600519")
    user2 = db.query(User).filter(User.id == "u2").one()

    out = create_alert_rule(_body(mine), db=db, user=user2)

    assert out["id"]
    assert db.query(PriceAlertRule).filter(PriceAlertRule.user_id == "u2").count() == 1


def test_api_pro账号不受普通配额约束(db):
    user = _user(db, "up", role="pro")
    _fill_rules(db, "up", 3)
    extra = _stock(db, "up", "600519")

    out = create_alert_rule(_body(extra), db=db, user=user)

    assert out["id"]
    assert db.query(PriceAlertRule).filter(PriceAlertRule.user_id == "up").count() == 4


def test_api_未超额仍可创建(db):
    user = _user(db, "u1")
    st = _stock(db, "u1", "600519")
    out = create_alert_rule(_body(st), db=db, user=user)
    assert out["stock_symbol"] == "600519"
    assert db.query(PriceAlertRule).count() == 1


# ── AI 动作路径 ───────────────────────────────────────────────────────


def test_ai_提醒超配额时拒绝且动作仍pending(db):
    _user(db, "u1")
    _fill_rules(db, "u1", 3)
    item = act.propose(db, user_id="u1", kind="alert", payload={"symbol": "600519", "price": 30})

    with pytest.raises(act.ActionError) as ei:
        act.apply_action(db, user_id="u1", action_id=item["id"])

    assert "预警上限" in str(ei.value)
    assert ei.value.status_code == 403
    assert db.query(PriceAlertRule).count() == 3, "被拒的动作不能多写一条规则"
    assert db.query(Stock).filter(Stock.symbol == "600519").count() == 0, "被拒时不能偷偷建自选"
    assert _pending(db, item["id"]).status == "pending", "配额拒绝后动作必须回到可重试状态"


def test_ai_提醒顺带加自选受自选配额约束(db):
    _user(db, "u1")
    for i in range(10):  # MEMBER_WATCHLIST_MAX 默认 10
        _stock(db, "u1", f"60{i:04d}")
    item = act.propose(db, user_id="u1", kind="alert", payload={"symbol": "600519", "price": 30})

    with pytest.raises(act.ActionError) as ei:
        act.apply_action(db, user_id="u1", action_id=item["id"])

    assert "自选上限" in str(ei.value)
    assert db.query(Stock).filter(Stock.user_id == "u1").count() == 10
    assert db.query(PriceAlertRule).count() == 0
    assert _pending(db, item["id"]).status == "pending"


def test_ai_提醒新建自选时文案披露副作用(db):
    _user(db, "u1")
    item = act.propose(
        db, user_id="u1", kind="alert",
        payload={"symbol": "600519", "price": 1688, "direction": "above"},
    )
    applied = act.apply_action(db, user_id="u1", action_id=item["id"])

    assert "一并加入自选" in applied["result"], "实际建了自选就必须在结果里说出来"
    assert db.query(Stock).filter(Stock.user_id == "u1", Stock.symbol == "600519").count() == 1
    assert db.query(PriceAlertRule).count() == 1


def test_ai_提醒已在自选时不声称加了自选(db):
    _user(db, "u1")
    _stock(db, "u1", "600519")
    item = act.propose(db, user_id="u1", kind="alert", payload={"symbol": "600519", "price": 30})
    applied = act.apply_action(db, user_id="u1", action_id=item["id"])

    assert "一并加入自选" not in applied["result"]
    assert db.query(Stock).filter(Stock.user_id == "u1").count() == 1


def test_ai_提醒diff披露会一并加自选(db):
    _user(db, "u1")
    d = act.preview_diff(db, user_id="u1", kind="alert",
                         payload={"symbol": "600519", "price": 30})
    fields = {x["field"]: x for x in d}
    assert "自选" in fields and fields["自选"]["change"] == "add"

    _stock(db, "u1", "600519")
    d2 = act.preview_diff(db, user_id="u1", kind="alert",
                          payload={"symbol": "600519", "price": 30})
    assert all("自选" != x["field"] for x in d2), "已在自选就不能说要加自选"


def test_ai_重复提醒不重复建规则也不多烧配额(db):
    _user(db, "u1")
    first = act.propose(db, user_id="u1", kind="alert",
                        payload={"symbol": "600519", "price": 30, "direction": "above"})
    act.apply_action(db, user_id="u1", action_id=first["id"])

    second = act.propose(db, user_id="u1", kind="alert",
                         payload={"symbol": "600519", "price": 32, "direction": "above"})
    applied = act.apply_action(db, user_id="u1", action_id=second["id"])

    rules = db.query(PriceAlertRule).filter(PriceAlertRule.user_id == "u1").all()
    assert len(rules) == 1, "同一标的重复盯一下应更新既有规则，而不是再占一条配额"
    assert rules[0].condition_group["items"][0]["value"] == 32
    assert "盯盘提醒" in applied["result"]


def test_ai_提醒只挂在同市场的自选上(db):
    _user(db, "u1")
    cn = _stock(db, "u1", "00700", market="CN")
    item = act.propose(db, user_id="u1", kind="alert",
                       payload={"symbol": "00700", "market": "HK", "price": 400})
    act.apply_action(db, user_id="u1", action_id=item["id"])

    hk = db.query(Stock).filter(Stock.user_id == "u1", Stock.market == "HK").all()
    assert len(hk) == 1 and hk[0].symbol == "00700"
    rule = db.query(PriceAlertRule).filter(PriceAlertRule.user_id == "u1").one()
    assert rule.stock_id == hk[0].id, "跨市场同代码不能复用到 CN 那条自选上"
    assert db.query(PriceAlertRule).filter(PriceAlertRule.stock_id == cn.id).count() == 0


def test_ai_不能执行他人的提醒动作(db):
    _user(db, "u1")
    _user(db, "u2")
    item = act.propose(db, user_id="u1", kind="alert", payload={"symbol": "600519", "price": 30})

    with pytest.raises(act.ActionError):
        act.apply_action(db, user_id="u2", action_id=item["id"])

    assert db.query(PriceAlertRule).count() == 0
    assert db.query(Stock).count() == 0, "他人动作不能替人建自选"
    assert _pending(db, item["id"]).status == "pending"


def test_ai_配额只数本人规则(db):
    _user(db, "u1")
    _user(db, "u2")
    for i in range(3):
        _rule(db, "u1", _stock(db, "u1", f"60{i:04d}"))
    item = act.propose(db, user_id="u2", kind="alert", payload={"symbol": "600519", "price": 30})

    applied = act.apply_action(db, user_id="u2", action_id=item["id"])

    assert applied["status"] == "applied"
    assert db.query(PriceAlertRule).filter(PriceAlertRule.user_id == "u2").count() == 1


def test_ai_撤销退回规则但保留顺带建的自选(db):
    """2026-11-07 复审 B2 改口径: 自选行**不删**。

    删 stocks 行会走 ORM `cascade="all, delete-orphan"` 连带删掉该用户后来挂在
    这只票上的 Position / StockAgent —— 为了让"撤销干净"而毁用户持仓，次序反了。
    撤销只撤本次动作**写下**的提醒，自选保留并在结果里明说。
    """
    _user(db, "u1")
    item = act.propose(db, user_id="u1", kind="alert", payload={"symbol": "600519", "price": 30})
    act.apply_action(db, user_id="u1", action_id=item["id"])
    assert db.query(Stock).count() == 1

    out = act.rollback_action(db, user_id="u1", action_id=item["id"], reason="建错了")

    assert db.query(PriceAlertRule).count() == 0
    assert db.query(Stock).count() == 1, "自选行保留, 避免级联删掉用户持仓/监控"
    assert "保留" in out["result"]


def test_ai_撤销不动原本就在的自选(db):
    _user(db, "u1")
    st = _stock(db, "u1", "600519")
    item = act.propose(db, user_id="u1", kind="alert", payload={"symbol": "600519", "price": 30})
    act.apply_action(db, user_id="u1", action_id=item["id"])

    act.rollback_action(db, user_id="u1", action_id=item["id"], reason="建错了")

    assert db.query(PriceAlertRule).count() == 0
    assert db.query(Stock).filter(Stock.id == st.id).count() == 1


# ── 提示词 / schema 与实际行为一致 ────────────────────────────────────


def test_propose_action_schema暴露direction():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    props = CHAT_TOOL_REGISTRY["propose_action"].schema["function"]["parameters"]["properties"]
    assert props["direction"]["enum"] == ["above", "below"]


def test_propose_action提示词不谎称仅记录():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    desc = CHAT_TOOL_REGISTRY["propose_action"].schema["function"]["description"]
    assert "暂不执行" not in desc and "仅记录" not in desc
    assert "自选" in desc and "配额" in desc


def test_propose工具结果按实际情况披露副作用(db):
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    _user(db, "u1")
    handler = CHAT_TOOL_REGISTRY["propose_action"].handler
    args = {"kind": "alert", "symbol": "600519", "price": 30}
    msg = asyncio.run(handler(db, dict(args), db.query(User).filter(User.id == "u1").one()))
    assert "自选" in msg and "一并加入" in msg

    _stock(db, "u1", "600519")
    msg2 = asyncio.run(handler(db, dict(args), db.query(User).filter(User.id == "u1").one()))
    assert "一并加入" not in msg2, "已在自选时不能提示会加自选"


# ── 同族漏洞: AI「加自选」也不能绕过自选配额 ──────────────────────────


def test_ai_加自选受自选配额约束(db):
    _user(db, "u1")
    for i in range(10):
        _stock(db, "u1", f"60{i:04d}")
    item = act.propose(db, user_id="u1", kind="watchlist", payload={"symbol": "600519"})

    with pytest.raises(act.ActionError) as ei:
        act.apply_action(db, user_id="u1", action_id=item["id"])

    assert "自选上限" in str(ei.value)
    assert db.query(Stock).filter(Stock.user_id == "u1").count() == 10
    assert _pending(db, item["id"]).status == "pending"


def test_ai_演示账号走guest自选上限(db):
    _user(db, "ug", role="guest", username="demo")
    _stock(db, "ug", "600519")
    item = act.propose(db, user_id="ug", kind="watchlist", payload={"symbol": "000001"})

    with pytest.raises(act.ActionError) as ei:
        act.apply_action(db, user_id="ug", action_id=item["id"])

    assert "演示账号" in str(ei.value), "演示上限与 POST /api/stocks 必须同口径"
    assert db.query(Stock).filter(Stock.user_id == "ug").count() == 1


def test_ai_账号行缺失按member口径判定(db):
    """没有 users 行时按 member(最严普通档)判定, 不因查不到账号就放行/升权。"""
    if os.environ.get("ALERT_TEST_PG_URL"):
        pytest.skip(
            "PostgreSQL 强制 stocks.user_id → users.id 外键，构造不出「有自选没有账号行」的库；"
            "该分支由 SQLite 用例覆盖（PG 上这条不是产品行为差异）"
        )
    for i in range(10):
        _stock(db, "ghost", f"60{i:04d}")
    item = act.propose(db, user_id="ghost", kind="watchlist", payload={"symbol": "600519"})

    with pytest.raises(act.ActionError) as ei:
        act.apply_action(db, user_id="ghost", action_id=item["id"])

    assert "自选上限" in str(ei.value)
    assert db.query(Stock).filter(Stock.user_id == "ghost").count() == 10


# ── 2026-11-07 复审阻断回归: 撤销误删 / 级联删除 / 覆盖既有规则 ─────────
#
# 三条硬约束（数据不丢优先于"撤销干净"）:
#   1. 撤销只删**本次动作记下的那条规则**（apply 时存 ID），同标的后建的提醒不动；
#      快照没有 ID 就 fail-closed（409），绝不按"最新一条"猜。
#   2. 撤销**不删**动作顺带建的自选 —— 删自选会 ORM 级联删掉用户后来加的持仓/
#      监控，宁可留一行自选也不碰用户数据；文案要写明。
#   3. apply 只能在"单条价格条件且启用中"的规则上就地更新；多条件/已停用的规则
#      保持原样，改为新增一条独立提醒，且 diff 要披露。

def _apply_alert_action(db, symbol="600519", price=30, direction="above", market="CN"):
    item = act.propose(
        db, user_id="u1", kind="alert",
        payload={"symbol": symbol, "price": price, "direction": direction, "market": market},
    )
    return item["id"], act.apply_action(db, user_id="u1", action_id=item["id"])


def test_撤销不删用户后续在同标的建的提醒(db):
    """B1: 动作建规则 A，用户随后建同标的规则 B —— 撤销只撤 A。"""
    _user(db, "u1")
    aid, _ = _apply_alert_action(db)
    stock = db.query(Stock).filter(Stock.user_id == "u1", Stock.symbol == "600519").one()

    rule_b = PriceAlertRule(
        user_id="u1", stock_id=stock.id, name="用户自己建的",
        enabled=True,
        condition_group={"op": "and", "items": [{"type": "price", "op": "<=", "value": 5.0}]},
    )
    db.add(rule_b)
    db.commit()
    bid = rule_b.id

    act.rollback_action(db, user_id="u1", action_id=aid, reason="建错了")

    left = db.query(PriceAlertRule).filter(PriceAlertRule.stock_id == stock.id).all()
    assert [r.id for r in left] == [bid], "只应删掉本次动作建的规则，用户后建的那条必须还在"
    assert left[0].condition_group["items"][0]["value"] == 5.0


def test_撤销保留顺带建的自选且不级联删用户持仓(db):
    """B2: 动作自建的自选行撤销后保留；用户后加的持仓/监控完好。"""
    from src.db.models import Account, Position, StockAgent

    _user(db, "u1")
    aid, applied = _apply_alert_action(db)
    assert "一并加入自选" in applied["result"]
    stock = db.query(Stock).filter(Stock.user_id == "u1", Stock.symbol == "600519").one()

    acct = Account(user_id="u1", name="招商证券")
    db.add(acct)
    db.commit()
    pos = Position(user_id="u1", account_id=acct.id, stock_id=stock.id,
                   cost_price=1600.0, quantity=100)
    agent = StockAgent(stock_id=stock.id, agent_name="intraday")
    db.add_all([pos, agent])
    db.commit()
    sid, pos_id, agent_id = stock.id, pos.id, agent.id

    out = act.rollback_action(db, user_id="u1", action_id=aid, reason="建错了")

    assert db.query(PriceAlertRule).count() == 0, "本次建的提醒仍应撤掉"
    assert db.query(Stock).filter(Stock.id == sid).count() == 1, "自选行必须保留（删除会级联毁掉持仓）"
    assert db.query(Position).filter(Position.id == pos_id).count() == 1, "用户持仓不能被级联删除"
    assert db.query(StockAgent).filter(StockAgent.id == agent_id).count() == 1
    assert "保留" in out["result"], "保留自选这件事必须写进撤销结果，不能静默"


def test_撤销对没有规则ID的历史快照fail_closed(db):
    """B1: 老记录只有"该标的最新一条"可猜 —— 拒绝猜，一行不删。"""
    import json as _json

    _user(db, "u1")
    aid, _ = _apply_alert_action(db)
    stock = db.query(Stock).filter(Stock.user_id == "u1", Stock.symbol == "600519").one()
    mine = PriceAlertRule(
        user_id="u1", stock_id=stock.id, name="用户的",
        condition_group={"op": "and", "items": [{"type": "price", "op": "<=", "value": 5.0}]},
    )
    db.add(mine)
    db.commit()
    before = db.query(PriceAlertRule).count()

    row = db.query(CopilotAction).filter(CopilotAction.id == aid).one()
    legacy = {"stock_existed": False, "stock_id": None, "stock_market": "CN",
              "rule_id": None, "rule_condition": None}
    row.before_snapshot = _json.dumps(legacy, ensure_ascii=False)
    db.commit()

    with pytest.raises(act.ActionError) as ei:
        act.rollback_action(db, user_id="u1", action_id=aid, reason="试试")

    assert ei.value.status_code == 409
    assert "fail-closed" in str(ei.value) or "拒绝" in str(ei.value)
    db.expire_all()
    assert db.query(PriceAlertRule).count() == before, "无法证明归属时不许删任何规则"
    assert db.query(CopilotAction).filter(CopilotAction.id == aid).one().status == "applied"


def test_撤销不删已被用户改过的本次规则(db):
    """B1 续: 本次建的规则被用户改过条件 —— 删它就是销毁用户编辑，fail-closed。"""
    _user(db, "u1")
    aid, _ = _apply_alert_action(db)
    rule = db.query(PriceAlertRule).one()
    rid = rule.id
    rule.condition_group = {"op": "and", "items": [{"type": "price", "op": ">=", "value": 999.0}]}
    db.commit()

    with pytest.raises(act.ActionError) as ei:
        act.rollback_action(db, user_id="u1", action_id=aid, reason="x")

    assert ei.value.status_code == 409
    db.expire_all()
    kept = db.query(PriceAlertRule).filter(PriceAlertRule.id == rid).one()
    assert kept.condition_group["items"][0]["value"] == 999.0


def test_多条件规则不被悄悄覆盖_改为新增并披露(db):
    """B3: 用户的复杂条件不能被一条价格条件覆盖掉。"""
    _user(db, "u1")
    stock = _stock(db, "u1", "600519")
    complex_rule = PriceAlertRule(
        user_id="u1", stock_id=stock.id, name="复合条件",
        condition_group={"op": "or", "items": [
            {"type": "price", "op": ">=", "value": 20.0},
            {"type": "price", "op": "<=", "value": 10.0},
        ]},
    )
    db.add(complex_rule)
    db.commit()
    cid = complex_rule.id

    diffs = act.preview_diff(db, user_id="u1", kind="alert",
                            payload={"symbol": "600519", "price": 30})
    assert any(d["field"] == "现有提醒" for d in diffs), "不更新既有规则时 diff 必须说明它会保持原样"
    cond_entry = next(d for d in diffs if "触发条件" in d["field"])
    assert cond_entry["change"] == "add"

    aid, _ = _apply_alert_action(db, price=30)

    db.expire_all()
    kept = db.query(PriceAlertRule).filter(PriceAlertRule.id == cid).one()
    assert kept.condition_group["op"] == "or"
    assert len(kept.condition_group["items"]) == 2, "复杂条件不能被单条价格条件覆盖"
    assert db.query(PriceAlertRule).filter(PriceAlertRule.stock_id == stock.id).count() == 2

    act.rollback_action(db, user_id="u1", action_id=aid, reason="建错了")
    db.expire_all()
    assert db.query(PriceAlertRule).filter(PriceAlertRule.id == cid).count() == 1, "撤销不能删用户的复合规则"


def test_停用规则不被悄悄重新启用(db):
    """B3: 用户停用的规则不能被 apply 顺手打开。"""
    _user(db, "u1")
    stock = _stock(db, "u1", "600519")
    off = PriceAlertRule(
        user_id="u1", stock_id=stock.id, name="已停用",
        enabled=False,
        condition_group={"op": "and", "items": [{"type": "price", "op": ">=", "value": 20.0}]},
    )
    db.add(off)
    db.commit()
    oid = off.id

    aid, _ = _apply_alert_action(db, price=30)

    db.expire_all()
    still = db.query(PriceAlertRule).filter(PriceAlertRule.id == oid).one()
    assert still.enabled is False, "停用状态属于用户意图，不能被覆盖并重新启用"
    assert still.condition_group["items"][0]["value"] == 20.0
    assert db.query(PriceAlertRule).filter(PriceAlertRule.stock_id == stock.id).count() == 2

    act.rollback_action(db, user_id="u1", action_id=aid, reason="建错了")
    db.expire_all()
    assert db.query(PriceAlertRule).filter(PriceAlertRule.id == oid).one().enabled is False


def test_就地更新后撤销完整恢复原条件与启用状态(db):
    """B3 续: 更新路径的撤销必须把条件/启用/名称都写回去。"""
    _user(db, "u1")
    stock = _stock(db, "u1", "600519")
    orig = PriceAlertRule(
        user_id="u1", stock_id=stock.id, name="原始提醒",
        enabled=True,
        condition_group={"op": "and", "items": [{"type": "price", "op": ">=", "value": 12.0}]},
    )
    db.add(orig)
    db.commit()
    oid = orig.id

    aid, _ = _apply_alert_action(db, price=30)
    db.expire_all()
    assert db.query(PriceAlertRule).count() == 1, "单条简单条件规则应就地更新"
    assert db.query(PriceAlertRule).filter(PriceAlertRule.id == oid).one().condition_group["items"][0]["value"] == 30

    act.rollback_action(db, user_id="u1", action_id=aid, reason="建错了")
    db.expire_all()
    back = db.query(PriceAlertRule).filter(PriceAlertRule.id == oid).one()
    assert back.condition_group["items"][0]["value"] == 12.0, "撤销必须恢复原触发条件"
    assert back.enabled is True
    assert back.name == "原始提醒"


def test_提议文案说明不会覆盖既有复合规则(db):
    """B3 披露: 工具结果必须念出"既有提醒保持原样，本次另建一条"，不只列新条件。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    _user(db, "u1")
    stock = _stock(db, "u1", "600519")
    db.add(PriceAlertRule(
        user_id="u1", stock_id=stock.id, name="复合条件",
        condition_group={"op": "or", "items": [
            {"type": "price", "op": ">=", "value": 20.0},
            {"type": "price", "op": "<=", "value": 10.0},
        ]},
    ))
    db.commit()

    msg = asyncio.run(CHAT_TOOL_REGISTRY["propose_action"].handler(
        db, {"kind": "alert", "symbol": "600519", "price": 30},
        db.query(User).filter(User.id == "u1").one(),
    ))

    assert "保持原样" in msg, "不覆盖既有规则这件事必须在提议里说出来"
    assert "新增" in msg and "配额" in msg


# ── 2026-11-07 二次复审: 虚假可撤销 / 错误撤销状态 / 未披露改名 / 一次性失效 ──
#
# 四条硬约束:
#   1. `can_rollback` 只在**实测**能撤成功时才为 true —— 老记录证明不出规则 ID、
#      规则已被改动/换了属主，一律 false（前端不许拿一个会 409 的按钮骗人）。
#   2. 老记录条件已被用户改过 → 撤销 409 **原状态不变**，绝不"只写个文字提示就
#      把动作标成 rolled_back"（那会让前端显示已撤销、实际什么都没撤）。
#   3. `rule.name` 会被 apply 改写 → diff 必须把"改名"这一项如实列出来。
#   4. 已过期 / 一次性已触发的规则**永远不会再触发** → 不许判为可更新（那等于写进去
#      一条永不触发的提醒）；改为新增独立提醒，并在 diff 里说明原规则为何保持原样。

def _legacy_action(db, *, snapshot: dict, payload: dict | None = None) -> int:
    """造一条"本次修复之前"的 applied 动作（老快照格式，没有 applied 段）。"""
    import json as _json

    item = act.propose(
        db, user_id="u1", kind="alert",
        payload=payload or {"symbol": "600519", "price": 30, "direction": "above", "market": "CN"},
    )
    aid = item["id"]
    act.apply_action(db, user_id="u1", action_id=aid)
    row = db.query(CopilotAction).filter(CopilotAction.id == aid).one()
    row.before_snapshot = _json.dumps(snapshot, ensure_ascii=False)
    db.commit()
    return aid


def test_过期规则不被判为可更新(db):
    """已过期规则永远不会触发 —— 更新它等于写进去一条永不触发的提醒。"""
    from datetime import datetime, timedelta

    _user(db, "u1")
    stock = _stock(db, "u1", "600519")
    old = PriceAlertRule(
        user_id="u1", stock_id=stock.id, name="已过期",
        enabled=True,
        condition_group={"op": "and", "items": [{"type": "price", "op": ">=", "value": 12.0}]},
        expire_at=datetime.utcnow() - timedelta(days=1),
    )
    db.add(old)
    db.commit()
    oid = old.id

    diffs = act.preview_diff(db, user_id="u1", kind="alert",
                             payload={"symbol": "600519", "price": 30})
    cond = next(d for d in diffs if "触发条件" in d["field"])
    assert cond["change"] == "add", "过期规则不能就地更新"
    kept = next(d for d in diffs if d["field"] == "现有提醒")
    assert "过期" in kept["before"], "diff 必须说明原规则为何不覆盖"

    _apply_alert_action(db, price=30)
    db.expire_all()
    assert db.query(PriceAlertRule).filter(PriceAlertRule.id == oid).one().expire_at is not None
    assert db.query(PriceAlertRule).count() == 2, "应新增一条能真正触发的提醒"


def test_一次性已触发规则不被判为可更新(db):
    """once 规则已触发（引擎对 once 只放行第一次）—— 改它等于永不触发。"""
    from datetime import datetime

    _user(db, "u1")
    stock = _stock(db, "u1", "600519")
    once = PriceAlertRule(
        user_id="u1", stock_id=stock.id, name="一次性已触发",
        enabled=True, repeat_mode="once", last_trigger_at=datetime.utcnow(),
        condition_group={"op": "and", "items": [{"type": "price", "op": ">=", "value": 12.0}]},
    )
    db.add(once)
    db.commit()
    oid = once.id

    diffs = act.preview_diff(db, user_id="u1", kind="alert",
                             payload={"symbol": "600519", "price": 30})
    assert next(d for d in diffs if "触发条件" in d["field"])["change"] == "add"
    assert "已触发" in next(d for d in diffs if d["field"] == "现有提醒")["before"]

    _apply_alert_action(db, price=30)
    db.expire_all()
    kept = db.query(PriceAlertRule).filter(PriceAlertRule.id == oid).one()
    assert kept.condition_group["items"][0]["value"] == 12.0, "原规则一个字都不能动"
    assert db.query(PriceAlertRule).count() == 2


def test_就地更新时改名必须进diff(db):
    """apply 会按 label 改名 —— 改名也是"会改什么"，diff 不能漏。"""
    _user(db, "u1")
    stock = _stock(db, "u1", "600519")
    db.add(PriceAlertRule(
        user_id="u1", stock_id=stock.id, name="原始提醒", enabled=True,
        condition_group={"op": "and", "items": [{"type": "price", "op": ">=", "value": 12.0}]},
    ))
    db.commit()

    diffs = act.preview_diff(db, user_id="u1", kind="alert",
                             payload={"symbol": "600519", "price": 30, "label": "突破提醒"})
    name_diff = next(d for d in diffs if "名称" in d["field"])
    assert name_diff["before"] == "原始提醒"
    assert name_diff["after"] == "突破提醒"
    assert name_diff["change"] == "update"

    # 不给 label 就不改名，也不该凭空多一条 diff
    diffs2 = act.preview_diff(db, user_id="u1", kind="alert",
                              payload={"symbol": "600519", "price": 30})
    assert not [d for d in diffs2 if "名称" in d["field"]]


def test_老记录条件被改后撤销必须409且不标rolled_back(db):
    """错误撤销状态: 只写"未覆盖"文字却把动作标成 rolled_back = 骗前端已撤销。"""
    _user(db, "u1")
    stock = _stock(db, "u1", "600519")
    rule = PriceAlertRule(
        user_id="u1", stock_id=stock.id, name="原始", enabled=True,
        condition_group={"op": "and", "items": [{"type": "price", "op": ">=", "value": 12.0}]},
    )
    db.add(rule)
    db.commit()
    rid = rule.id

    aid = _legacy_action(db, snapshot={
        "stock_existed": True, "stock_id": stock.id, "stock_market": "CN",
        "rule_id": rid, "rule_condition": rule.condition_group,
        "rule_enabled": True, "rule_name": "原始", "rule_updatable": True,
    })
    # 用户在动作之后自己改了条件 → 已接管这条规则
    rule.condition_group = {"op": "and", "items": [{"type": "price", "op": "<=", "value": 5.0}]}
    db.commit()

    with pytest.raises(act.ActionError) as ei:
        act.rollback_action(db, user_id="u1", action_id=aid, reason="x")
    assert ei.value.status_code == 409

    db.expire_all()
    assert db.query(CopilotAction).filter(CopilotAction.id == aid).one().status == "applied", \
        "撤销失败必须保持 applied，不能标成 rolled_back"
    kept = db.query(PriceAlertRule).filter(PriceAlertRule.id == rid).one()
    assert kept.condition_group["items"][0]["value"] == 5.0, "用户改动必须原样保留"


def test_can_rollback只在实测能撤时为true(db):
    """老记录无 ID / 规则被改过 → can_rollback 必须 false（不许给会 409 的按钮）。"""
    _user(db, "u1")
    stock = _stock(db, "u1", "600519")
    rule = PriceAlertRule(
        user_id="u1", stock_id=stock.id, name="原始", enabled=True,
        condition_group={"op": "and", "items": [{"type": "price", "op": ">=", "value": 12.0}]},
    )
    db.add(rule)
    db.commit()
    rid = rule.id

    # 1) 新记录（有 applied 段）→ 真能撤
    aid_new, _ = _apply_alert_action(db, price=40)
    row = db.query(CopilotAction).filter(CopilotAction.id == aid_new).one()
    assert act._to_dict(row, db=db)["can_rollback"] is True

    # 2) 老记录没有可证 ID → false
    aid_legacy = _legacy_action(db, snapshot={
        "stock_existed": False, "stock_id": None, "stock_market": "CN",
        "rule_id": None, "rule_condition": None,
    })
    row = db.query(CopilotAction).filter(CopilotAction.id == aid_legacy).one()
    assert act._to_dict(row, db=db)["can_rollback"] is False, \
        "没有可证 ID 的老记录不许声称可撤"

    # 3) 本次规则已被用户改过 → false（撤销会 409）
    own = db.query(PriceAlertRule).filter(PriceAlertRule.id == rid).one()
    own.condition_group = {"op": "and", "items": [{"type": "price", "op": ">=", "value": 999.0}]}
    db.commit()
    row = db.query(CopilotAction).filter(CopilotAction.id == aid_new).one()
    assert act._to_dict(row, db=db)["can_rollback"] is False

    # 4) 没有 session 就证明不了 → 一律 false
    assert act._to_dict(row)["can_rollback"] is False
