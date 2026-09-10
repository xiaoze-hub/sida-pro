"""M7(2026-09-10) 定时 Agent 多用户隔离回归。

修的问题: 调度路径 `build_context(agent_name)` 不带用户 → 一个 job 把**所有用户**绑定到
该 agent 的自选混成一份 prompt, 建议/历史以 `user_id=None` 落库(= 人人可见的共享行) →
A 能看到 B 持仓标的的建议。

覆盖:
  - `agent_user_buckets`: 按绑定标的归属拆分用户桶
  - `load_watchlist_for_agent(user_id=...)`: 只取该用户/遗留共享桶的标的
  - `AgentScheduler._build_contexts`: 按用户桶构建 + 空自选跳过 + 旧签名回退
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import Base, Stock, StockAgent, User

U1 = "11111111-0000-0000-0000-000000000001"
U2 = "22222222-0000-0000-0000-000000000002"


def _mk_session():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        eng, tables=[User.__table__, Stock.__table__, StockAgent.__table__]
    )
    return sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)


def _seed(S):
    db = S()
    db.add_all(
        [
            User(id=U1, username="u1", password_hash="x", role="member", is_active=True, token_version=1),
            User(id=U2, username="u2", password_hash="x", role="member", is_active=True, token_version=1),
        ]
    )
    db.add_all(
        [
            Stock(id=1, symbol="600519", name="茅台", market="CN", user_id=U1),
            Stock(id=2, symbol="000001", name="平安", market="CN", user_id=U1),
            Stock(id=3, symbol="300750", name="宁德", market="CN", user_id=U2),
            Stock(id=4, symbol="600000", name="浦发", market="CN", user_id=None),  # 遗留共享
        ]
    )
    db.add_all(
        [
            StockAgent(id=1, stock_id=1, agent_name="premarket_outlook", schedule=""),
            StockAgent(id=2, stock_id=2, agent_name="premarket_outlook", schedule=""),
            StockAgent(id=3, stock_id=3, agent_name="premarket_outlook", schedule=""),
            StockAgent(id=4, stock_id=4, agent_name="premarket_outlook", schedule=""),
            StockAgent(id=5, stock_id=3, agent_name="other_agent", schedule=""),
        ]
    )
    db.commit()
    db.close()


def _patch_session(monkeypatch, S):
    import src.bootstrap.runtime as rt

    monkeypatch.setattr(rt, "SessionLocal", S)


def test_agent_user_buckets_distinct_owners(monkeypatch):
    S = _mk_session()
    _seed(S)
    _patch_session(monkeypatch, S)

    from src.bootstrap.runtime import agent_user_buckets

    buckets = agent_user_buckets("premarket_outlook")
    # 4 只标的归属 U1/U1/U2/None → 去重保序三个桶
    assert buckets == [U1, U2, None]
    # 未绑定该 agent 的标的(300750 也绑了 other_agent)不干扰
    assert agent_user_buckets("other_agent") == [U2]
    assert agent_user_buckets("no_such_agent") == []


def test_load_watchlist_scoped_by_user(monkeypatch):
    S = _mk_session()
    _seed(S)
    _patch_session(monkeypatch, S)

    from src.bootstrap.runtime import load_watchlist_for_agent

    u1 = {s.symbol for s in load_watchlist_for_agent("premarket_outlook", U1)}
    u2 = {s.symbol for s in load_watchlist_for_agent("premarket_outlook", U2)}
    shared = {s.symbol for s in load_watchlist_for_agent("premarket_outlook", None)}
    allsym = {s.symbol for s in load_watchlist_for_agent("premarket_outlook")}

    assert u1 == {"600519", "000001"}       # 只本人
    assert u2 == {"300750"}
    assert shared == {"600000"}             # 只遗留共享桶
    assert allsym == {"600519", "000001", "300750", "600000"}  # 旧调用不受影响
    assert u1.isdisjoint(u2)                # 关键: 跨用户不串号


def test_build_contexts_splits_by_user_and_skips_empty():
    from src.core.scheduler import AgentScheduler

    class _Ctx:
        def __init__(self, n: int):
            self.watchlist = list(range(n))
            self.portfolio = None
            self.model_label = ""

    sched = AgentScheduler()
    sched.set_context_builder(lambda name, uid=None: _Ctx(0 if uid == "empty" else 1))
    sched.set_user_bucket_resolver(lambda name: ["u1", "empty", "u2"])
    ctxs = sched._build_contexts("a")
    assert len(ctxs) == 2  # 空自选桶被跳过


def test_build_contexts_fallbacks():
    from src.core.scheduler import AgentScheduler

    class _Ctx:
        def __init__(self, n: int):
            self.watchlist = list(range(n))
            self.portfolio = None
            self.model_label = ""

    # 无解析器 → 单次全量
    s1 = AgentScheduler()
    s1.set_context_builder(lambda name: _Ctx(3))
    assert len(s1._build_contexts("a")) == 1

    # 解析器给了桶但 builder 是旧签名(只吃 agent_name) → TypeError 回退单次
    s2 = AgentScheduler()
    s2.set_context_builder(lambda name: _Ctx(2))
    s2.set_user_bucket_resolver(lambda name: ["u1"])
    assert len(s2._build_contexts("a")) == 1
