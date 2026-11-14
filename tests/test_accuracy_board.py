"""方向3回归: 分 Agent 命中榜(2026-11-12 P0 起按登录用户统计)。

历史教训: `agent_prediction_outcomes` 原先没有 user_id, 命中榜只能"全平台"混算 —— 两个
账号同标的同日的预测落同一个键, 还会互相覆盖。现在这张表按用户隔离, 因此:
  - 榜单只算当前账号的行;
  - 无归属的历史行(user_id 为空)不纳入成绩, 但要如实报出被排除的条数。
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M  # noqa: F401
from src.web.api import profile as profile_api
from src.web.api.auth import get_current_user
from src.web.database import Base, get_db
from src.web.models import AgentPredictionOutcome, User


class _User:
    id = "u1"
    username = "admin"
    role = "owner"
    nickname = None
    avatar = None
    shadow_profile_json = None


def _client(monkeypatch) -> TestClient:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(profile_api.router, prefix="/p")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    async def _u():
        return _User()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _u
    c = TestClient(app, raise_server_exceptions=False)
    c._db_session = Session()
    return c


def _add(
    s,
    agent,
    action,
    ret,
    day,
    user_id: str | None = "u1",
    symbol: str = "600519",
    owner_scope: str | None = "user",
):
    """一条已评估记录。注意: 加 user 维度后 (user, agent, symbol, market, date, horizon)
    是计分行唯一键 —— 每个样本必须是不同的标的/日期, 这正是"一天一只票一条预测"的口径。"""
    s.add(AgentPredictionOutcome(
        user_id=user_id, owner_scope=owner_scope, agent_name=agent, stock_symbol=symbol,
        stock_market="CN", prediction_date=day, horizon_days=1, action=action,
        outcome_return_pct=ret, outcome_status="evaluated",
    ))


def test_accuracy_board_splits_agents(monkeypatch):
    c = _client(monkeypatch)
    s = c._db_session
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=60)).isoformat()
    # good-agent: 6 中 5; bad-agent: 6 中 1; one-shot: 1 中 1(小样本); watch 不计
    for i in range(5):
        _add(s, "good", "buy", 2.0, today, symbol=f"60000{i}")
    _add(s, "good", "buy", -1.0, today, symbol="600010")
    _add(s, "bad", "buy", 2.0, today, symbol="600020")
    for i in range(5):
        _add(s, "bad", "buy", -1.0, today, symbol=f"60002{i + 1}")
    _add(s, "one-shot", "sell", -3.0, today, symbol="600030")
    _add(s, "good", "watch", 9.0, today, symbol="600040")  # 中性不计分母
    _add(s, "good", "buy", 9.0, old, symbol="600050")  # 窗口外
    s.commit()

    r = c.get("/p/stats/accuracy", params={"days": 30, "min_n": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    by = {a["agent"]: a for a in body["agents"]}
    assert by["good"]["hit_rate"] == round(5 / 6 * 100, 1) and by["good"]["total"] == 6
    assert by["good"]["qualified"] is True
    assert by["bad"]["hit_rate"] == round(1 / 6 * 100, 1)
    assert by["one-shot"]["qualified"] is False  # 1 样本不参评
    assert body["agents"][0]["agent"] == "good"  # 达标且最高在前
    assert body["overall"]["total"] == 13  # 6+6+1, watch 与窗外不计
    assert body["scope"] == "user" and body["user_scoped"] is True
    assert body["legacy_unscoped_rows"] == 0 and body["legacy_untrusted"] is False


def test_other_users_rows_and_legacy_rows_do_not_enter_the_board(monkeypatch):
    """别的账号的行不能进"我的命中率"; 无归属历史行排除 + 明确标注不可信。"""
    c = _client(monkeypatch)
    s = c._db_session
    today = date.today().isoformat()
    _add(s, "good", "buy", 2.0, today, user_id="u1")
    _add(s, "good", "buy", 2.0, today, user_id="u2")  # 别人的
    _add(s, "good", "buy", 2.0, today, user_id=None, owner_scope=None)  # 加列前的历史行
    _add(s, "good", "buy", -5.0, today, user_id=None, owner_scope=None)
    s.commit()

    body = c.get("/p/stats/accuracy", params={"days": 30, "min_n": 1}).json()
    assert body["overall"]["total"] == 1, "别的账号/无归属的行被算进了本账号成绩"
    assert body["overall"]["hit_count"] == 1
    assert body["legacy_unscoped_rows"] == 2
    assert body["legacy_untrusted"] is True
    assert "不纳入成绩" in body["note"] and "不可视为已修复" in body["note"]
    assert [a["agent"] for a in body["agents"]] == ["good"]


def test_system_bucket_is_counted_separately_and_never_merged(monkeypatch):
    """system 桶(无会话的系统运行)独立计数, 绝不并入任何账号的命中率。"""
    c = _client(monkeypatch)
    s = c._db_session
    today = date.today().isoformat()
    _add(s, "good", "buy", 2.0, today, user_id="u1", symbol="600101")
    _add(s, "good", "buy", -9.0, today, user_id=None, owner_scope="system", symbol="600102")
    _add(s, "good", "buy", 8.0, today, user_id=None, owner_scope="system", symbol="600103")
    s.commit()

    body = c.get("/p/stats/accuracy", params={"days": 30, "min_n": 1}).json()
    assert body["overall"]["total"] == 1, "system 桶被并进了账号成绩"
    assert body["overall"]["hit_count"] == 1
    assert body["system_scoped_rows"] == 2
    assert body["legacy_unscoped_rows"] == 0, "system 行不能被当成'历史未知归属'"
    assert "system 桶" in body["note"]


def test_legacy_stats_shape_kept(monkeypatch):
    """/stats 里 prediction 四键齐全(旧前端不崩), 只多 avg_return_pct 等字段。"""
    c = _client(monkeypatch)
    r = c.get("/p/stats")
    assert r.status_code == 200, r.text
    p = r.json()["prediction"]
    assert {"hit_count", "total", "hit_rate", "scope"} <= set(p)
