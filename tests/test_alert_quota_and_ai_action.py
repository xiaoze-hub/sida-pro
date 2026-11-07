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


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
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


def test_ai_撤销把规则和顺带建的自选一起退回(db):
    _user(db, "u1")
    item = act.propose(db, user_id="u1", kind="alert", payload={"symbol": "600519", "price": 30})
    act.apply_action(db, user_id="u1", action_id=item["id"])
    assert db.query(Stock).count() == 1

    act.rollback_action(db, user_id="u1", action_id=item["id"], reason="建错了")

    assert db.query(PriceAlertRule).count() == 0
    assert db.query(Stock).count() == 0, "动作自建的自选必须随撤销一起退回（配额才能释放）"


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
    for i in range(10):
        _stock(db, "ghost", f"60{i:04d}")
    item = act.propose(db, user_id="ghost", kind="watchlist", payload={"symbol": "600519"})

    with pytest.raises(act.ActionError) as ei:
        act.apply_action(db, user_id="ghost", action_id=item["id"])

    assert "自选上限" in str(ei.value)
    assert db.query(Stock).filter(Stock.user_id == "ghost").count() == 10
