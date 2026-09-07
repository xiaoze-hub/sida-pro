"""方向3回归: 分 Agent 命中榜(口径与旧全局统计一致 + 小样本保护)。"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import Depends, FastAPI
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


def _add(s, agent, action, ret, day):
    s.add(AgentPredictionOutcome(
        agent_name=agent, stock_symbol="600519", stock_market="CN",
        prediction_date=day, horizon_days=1, action=action,
        outcome_return_pct=ret, outcome_status="evaluated",
    ))


def test_accuracy_board_splits_agents(monkeypatch):
    c = _client(monkeypatch)
    s = c._db_session
    today = date.today().isoformat()
    old = (date.today() - timedelta(days=60)).isoformat()
    # good-agent: 6 中 5; bad-agent: 6 中 1; one-shot: 1 中 1(小样本); watch 不计
    for i in range(5):
        _add(s, "good", "buy", 2.0, today)
    _add(s, "good", "buy", -1.0, today)
    _add(s, "bad", "buy", 2.0, today)
    for i in range(5):
        _add(s, "bad", "buy", -1.0, today)
    _add(s, "one-shot", "sell", -3.0, today)
    _add(s, "good", "watch", 9.0, today)  # 中性不计分母
    _add(s, "good", "buy", 9.0, old)  # 窗口外
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
    assert body["scope"] == "global"


def test_legacy_stats_shape_kept(monkeypatch):
    """/stats 里 prediction 四键齐全(旧前端不崩), 只多 avg_return_pct。"""
    c = _client(monkeypatch)
    r = c.get("/p/stats")
    assert r.status_code == 200, r.text
    p = r.json()["prediction"]
    assert {"hit_count", "total", "hit_rate", "scope"} <= set(p)
