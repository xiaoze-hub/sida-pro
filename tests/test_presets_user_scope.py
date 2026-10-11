"""P1(2026-10-11) presets 多用户隔离回归。

修的问题: `src/web/api/presets.py:run()` 找标的时
`db.execute(select(Stock).where(Stock.symbol==target, Stock.market==market))`
**漏 user_id 过滤** → 两用户持有同标的时 `scalar_one_or_none()` 抛
MultipleResultsFound(500), 或命中他人标的(把别人的自选喂给本用户的分析)。

覆盖:
  - run() 只认当前用户的标的(本人自选 + 全局共享 user_id NULL), 绝不串到他人
  - 两用户同标的并存不再 500 (旧实现必炸 MultipleResultsFound)
  - 都无该标的 → 404 (不越权泄露)
"""
from __future__ import annotations

import sys
import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import Base, Stock, User

U1 = "11111111-0000-0000-0000-000000000001"
U2 = "22222222-0000-0000-0000-000000000002"


def _mk_session():
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[User.__table__, Stock.__table__])
    return sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)


def _seed_same_symbol(S):
    """两用户 + 一个全局共享, 持有**同一** symbol/market。"""
    db = S()
    db.add_all([
        User(id=U1, username="u1", password_hash="x", role="member", is_active=True, token_version=1),
        User(id=U2, username="u2", password_hash="x", role="member", is_active=True, token_version=1),
    ])
    db.add_all([
        Stock(id=1, symbol="600519", name="茅台-U1", market="CN", user_id=U1),
        Stock(id=2, symbol="600519", name="茅台-U2", market="CN", user_id=U2),
        Stock(id=3, symbol="600519", name="茅台-共享", market="CN", user_id=None),
    ])
    db.commit()
    db.close()


def _user(uid: str) -> User:
    return User(id=uid, username=uid, password_hash="x", role="member", is_active=True, token_version=1)


def _fake_preset():
    a = types.SimpleNamespace(id="market", role="分析师", system_prompt="p")
    return types.SimpleNamespace(
        name="a_share_team",
        title="A股团队",
        description="d",
        agents=[a],
        debate_rounds=1,
        selected_analysts=["market"],
    )


def _install_fakes(monkeypatch, S, captured):
    """把 run() 里的重活(DB / preset / TradingAgents / server)换成桩。"""
    import src.web.database as dbmod

    monkeypatch.setattr(dbmod, "SessionLocal", S)

    import src.web.api.presets as presets

    monkeypatch.setattr(presets, "get_preset", lambda name: _fake_preset())

    class _FakeAgent:
        def __init__(self, analyst_types=None, debate_rounds=None):
            pass

        async def analyze(self, context, data):
            captured["stock"] = data["stock"]
            return types.SimpleNamespace(ok=True)

    import src.agents.tradingagents.agent as agent_mod

    monkeypatch.setattr(agent_mod, "TradingAgentsAgent", _FakeAgent)

    import src.agents.base as base_mod

    monkeypatch.setattr(base_mod, "AgentContext", object)

    fake_server = types.ModuleType("server")
    fake_server.build_context = lambda agent_name=None: object()
    monkeypatch.setitem(sys.modules, "server", fake_server)


def test_run_picks_own_stock_not_other_user(monkeypatch):
    S = _mk_session()
    _seed_same_symbol(S)
    captured: dict = {}
    _install_fakes(monkeypatch, S, captured)

    from src.web.api.presets import RunPresetRequest, run
    import asyncio

    asyncio.run(run("a_share_team", RunPresetRequest(target="600519", market="CN"), _user(U1)))

    assert "stock" in captured
    # 关键: U1 拿到的是自己的(id=1), 不是 U2 的(id=2)也不是共享的(id=3)
    assert captured["stock"].id == 1
    assert captured["stock"].user_id == U1


def test_run_u2_gets_own_not_u1(monkeypatch):
    S = _mk_session()
    _seed_same_symbol(S)
    captured: dict = {}
    _install_fakes(monkeypatch, S, captured)

    from src.web.api.presets import RunPresetRequest, run
    import asyncio

    asyncio.run(run("a_share_team", RunPresetRequest(target="600519", market="CN"), _user(U2)))
    assert captured["stock"].id == 2
    assert captured["stock"].user_id == U2


def test_run_only_shared_falls_back_to_shared(monkeypatch):
    """该用户无自选但存在全局共享同标的 → 命中共享(不 500)。"""
    S = _mk_session()
    db = S()
    db.add(User(id=U2, username="u2", password_hash="x", role="member", is_active=True, token_version=1))
    db.add(Stock(id=9, symbol="600519", name="茅台-共享", market="CN", user_id=None))
    db.commit()
    db.close()

    captured: dict = {}
    _install_fakes(monkeypatch, S, captured)

    from src.web.api.presets import RunPresetRequest, run
    import asyncio

    asyncio.run(run("a_share_team", RunPresetRequest(target="600519", market="CN"), _user(U2)))
    assert captured["stock"].id == 9
    assert captured["stock"].user_id is None


def test_run_other_users_stock_is_404(monkeypatch):
    """只存在他人自选 → 404 (不把别人的标的喂给本用户, 也不 500)。"""
    from fastapi import HTTPException

    S = _mk_session()
    db = S()
    db.add(User(id=U1, username="u1", password_hash="x", role="member", is_active=True, token_version=1))
    db.add(User(id=U2, username="u2", password_hash="x", role="member", is_active=True, token_version=1))
    db.add(Stock(id=5, symbol="000001", name="平安-U2", market="CN", user_id=U2))
    db.commit()
    db.close()

    captured: dict = {}
    _install_fakes(monkeypatch, S, captured)

    from src.web.api.presets import RunPresetRequest, run
    import asyncio

    with pytest.raises(HTTPException) as ei:
        asyncio.run(run("a_share_team", RunPresetRequest(target="000001", market="CN"), _user(U1)))
    assert ei.value.status_code == 404
    assert "stock" not in captured
