"""动作 dry-run diff + 回滚（P1-1.3）。

钉住四件事：
  1. propose 就要有字段级 diff（用户点确认前必须看到"会改什么"）；
  2. **未 confirm 绝不落库**（propose 只写 pending 行，不碰业务表）；
  3. apply 后能回滚到 apply 前，且回滚写审计；
  4. 缺值用 None，**不写成 0**（"没设置"和"设为 0"是两件事）。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import Base, AuditLog, CopilotAction, PriceAlertRule, Stock
from src.harness import actions as act


@pytest.fixture()
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    # 用 create_all：`Stock` 落库时会碰到 stock_agents 等关联表（懒加载），
    # 只建四张表会在 apply 阶段报 "no such table"
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    yield s
    s.close()


def test_自选diff_显示会加什么(db):
    d = act.preview_diff(db, user_id="u1", kind="watchlist", payload={"symbol": "600111"})
    assert d and d[0]["change"] == "add"
    assert d[0]["before"] is None, "没在自选就要是 None，不能写成 0/空串"
    assert d[0]["after"] == "加入自选"


def test_自选已在则标noop(db):
    db.add(Stock(user_id="u1", symbol="600111", name="北方稀土", market="CN"))
    db.commit()
    d = act.preview_diff(db, user_id="u1", kind="watchlist", payload={"symbol": "600111"})
    assert d[0]["change"] == "noop"


def test_提醒diff带前后触发条件(db):
    d = act.preview_diff(db, user_id="u1", kind="alert",
                         payload={"symbol": "600111", "price": 40, "direction": "above"})
    assert d[0]["after"] == ">= 40"
    assert d[0]["before"] is None
    assert d[0]["change"] == "add"


def test_propose不落业务表(db):
    out = act.propose(db, user_id="u1", kind="watchlist", payload={"symbol": "600519"})
    assert out["status"] == "pending"
    assert out["diff"] and out["diff"][0]["change"] == "add"
    assert db.query(Stock).count() == 0, "propose 绝不能直接改自选（必须等 confirm）"


def test_apply后才落库(db):
    out = act.propose(db, user_id="u1", kind="watchlist", payload={"symbol": "600519"})
    applied = act.apply_action(db, user_id="u1", action_id=out["id"])
    assert applied["status"] == "applied"
    assert db.query(Stock).filter(Stock.symbol == "600519").count() == 1


def test_回滚删除本次新增的自选(db):
    out = act.propose(db, user_id="u1", kind="watchlist", payload={"symbol": "600519"})
    act.apply_action(db, user_id="u1", action_id=out["id"])
    rb = act.rollback_action(db, user_id="u1", action_id=out["id"], reason="选错标的")
    assert rb["status"] == "rolled_back"
    assert db.query(Stock).filter(Stock.symbol == "600519").count() == 0


def test_回滚不能碰本来就在的自选(db):
    db.add(Stock(user_id="u1", symbol="600111", name="北方稀土", market="CN"))
    db.commit()
    out = act.propose(db, user_id="u1", kind="watchlist", payload={"symbol": "600111"})
    act.apply_action(db, user_id="u1", action_id=out["id"])
    act.rollback_action(db, user_id="u1", action_id=out["id"])
    assert db.query(Stock).filter(Stock.symbol == "600111").count() == 1, "原本就在自选的不能被删掉"


def test_回滚写审计(db):
    out = act.propose(db, user_id="u1", kind="watchlist", payload={"symbol": "600519"})
    act.apply_action(db, user_id="u1", action_id=out["id"])
    act.rollback_action(db, user_id="u1", action_id=out["id"], reason="测试")
    logs = db.query(AuditLog).filter(AuditLog.action == "copilot_action_rollback").all()
    assert len(logs) == 1
    detail = json.loads(logs[0].detail)
    assert detail["action_id"] == out["id"]
    assert detail["reason"] == "测试"


def test_未apply的动作不能回滚(db):
    out = act.propose(db, user_id="u1", kind="watchlist", payload={"symbol": "600519"})
    with pytest.raises(act.ActionError) as ei:
        act.rollback_action(db, user_id="u1", action_id=out["id"])
    assert "applied" in str(ei.value)


def test_别人的动作不能回滚(db):
    out = act.propose(db, user_id="u1", kind="watchlist", payload={"symbol": "600519"})
    act.apply_action(db, user_id="u1", action_id=out["id"])
    with pytest.raises(act.ActionError):
        act.rollback_action(db, user_id="u2", action_id=out["id"])


def test_can_rollback只有applied才有(db):
    out = act.propose(db, user_id="u1", kind="watchlist", payload={"symbol": "600519"})
    assert out["can_rollback"] is False
    applied = act.apply_action(db, user_id="u1", action_id=out["id"])
    assert applied["can_rollback"] is True


def test_不支持diff的动作返回空而不是报错(db):
    assert act.preview_diff(db, user_id="u1", kind="annotation", payload={"symbol": "600111"}) == []
