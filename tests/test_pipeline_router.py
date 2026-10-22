"""管线路由器 + 单轮缓存接线（2026-10-23）。

两件事在这里守：

1. **路由分歧不外溢到 chat.py**：`chat.py` 有行数护栏（2307/2310），只够改 import 一行。
   `pipeline_router` 按 `recipe.scope` 把请求分给符号管线或全市场管线 ——
   这里验证分派正确、且两条管线的入口签名可以互换。
2. **缓存真的接上了**：`bar_cache` 单独测通了不算数，要验「核心读库入口在作用域内
   确实只打一次 PG」。所以用假 engine 数 connect()，跑真实 `trend_profile.build`。
"""
import asyncio
from datetime import datetime

import pytest

import src.harness.bar_cache as bc
from src.harness import pipeline_router as router


class _Recipe:
    def __init__(self, scope="symbol"):
        self.id = "t_router"
        self.scope = scope
        self.tools = ("a",)
        self.output_sections = ("结论",)
        self.prompt_fragment = "你是测试员。"
        self.max_tool_calls = 5


class _FakeEngine:
    """最小 engine 替身：只数 connect() 次数（= 打了几次 PG）。"""

    def __init__(self, rows=None):
        self.queries = 0
        self.rows = rows or [
            (datetime(2026, 10, 19), 10.0, 10.5, 9.8, 10.2, 1000.0, "tq"),
            (datetime(2026, 10, 20), 10.2, 10.9, 10.1, 10.8, 1200.0, "tq"),
        ]

    def connect(self):
        engine = self

        class _Ctx:
            def __enter__(self):
                engine.queries += 1

                class _Conn:
                    def execute(self, _stmt, _params=None):
                        return self

                    def fetchall(self):
                        return list(engine.rows)

                return _Conn()

            def __exit__(self, *_exc):
                return False

        return _Ctx()


# ── 1. scope 判定 ───────────────────────────────────────────────────────────


def test_market_scope_detection():
    assert router.is_market_scope(_Recipe("market")) is True
    assert router.is_market_scope(_Recipe("MARKET")) is True, "大小写不该影响判定"
    assert router.is_market_scope(_Recipe("symbol")) is False
    assert router.is_market_scope(_Recipe(None)) is False, "缺失 scope 必须按旧行为（符号管线）"


# ── 2. watchlist 解析 ───────────────────────────────────────────────────────


def test_resolve_watchlist_caps_and_dedupes():
    class _Row:
        def __init__(self, s):
            self.symbol = s

    class _Q:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *_a, **_k):
            return self

        def order_by(self, *_a, **_k):
            return self

        def limit(self, *_a, **_k):
            return self

        def all(self):
            return self._rows

    class _DB:
        def __init__(self, rows):
            self._rows = rows

        def query(self, *_a, **_k):
            return _Q(self._rows)

    rows = [_Row("600111"), _Row("600111"), _Row(""), _Row("002050")] + [
        _Row(f"30000{i}") for i in range(10)
    ]
    got = router.resolve_watchlist(_DB(rows), type("U", (), {"id": 1})())
    assert got[0] == "600111" and got[1] == "002050", "顺序保留、去重、跳过空代码"
    assert len(got) == router.MAX_WATCHLIST, "必须封顶，避免 8 只以上把下游打爆"
    assert router.resolve_watchlist(None, None) == [], "没有 db 时是空表（不是报错）"


def test_resolve_watchlist_never_raises():
    class _Boom:
        def query(self, *_a, **_k):
            raise RuntimeError("db down")

    assert router.resolve_watchlist(_Boom(), None) == []


# ── 3. 分派 ─────────────────────────────────────────────────────────────────


def test_answer_dispatches_to_market_pipeline(monkeypatch):
    seen = {}

    async def _fake_market(ai_client, recipe, executor, question, **kw):
        seen["market"] = kw.get("watchlist")
        return "market-answer"

    async def _fake_symbol(*_a, **_k):
        seen["symbol"] = True
        return "symbol-answer"

    monkeypatch.setattr(router._mkt, "answer_market", _fake_market)
    monkeypatch.setattr(router._sym, "answer", _fake_symbol)
    monkeypatch.setattr(router, "resolve_watchlist", lambda db, user: ["600111"])

    out = asyncio.run(router.answer(None, _Recipe("market"), "盘前怎么看", None, None))
    assert out == "market-answer"
    assert seen["market"] == ["600111"], "全市场管线必须拿到 watchlist"
    assert "symbol" not in seen

    out = asyncio.run(router.answer(None, _Recipe("symbol"), "600111 怎么样", "600111", None))
    assert out == "symbol-answer" and seen.get("symbol") is True


def test_answer_stream_dispatches_to_market_pipeline(monkeypatch):
    async def _fake_market_stream(ai_client, recipe, executor, question, **kw):
        yield "stage", "正在取数 1/2：get_hot_stocks"
        yield "delta", "结论"
        yield "done", "结论"

    async def _fake_symbol_stream(*_a, **_k):
        yield "done", "不该走到这里"

    monkeypatch.setattr(router._mkt, "answer_market_stream", _fake_market_stream)
    monkeypatch.setattr(router._sym, "answer_stream", _fake_symbol_stream)

    events = []

    async def go():
        async for k, v in router.answer_stream(None, _Recipe("market"), "复盘", None, None):
            events.append((k, v))

    asyncio.run(go())
    assert [k for k, _ in events] == ["stage", "delta", "done"]


def test_answer_stream_market_all_fail_yields_nothing(monkeypatch):
    """全市场管线取不到任何维度 → 一个事件都不产出（调用方回落多轮 loop）。"""
    async def _empty(*_a, **_k):
        if False:
            yield None

    monkeypatch.setattr(router._mkt, "answer_market_stream", _empty)

    events = []

    async def go():
        async for ev in router.answer_stream(None, _Recipe("market"), "x", None, None):
            events.append(ev)

    asyncio.run(go())
    assert events == []


# ── 4. 缓存接线：核心读库入口在作用域内只打一次 ────────────────────────────


def test_trend_profile_build_uses_scope_cache(monkeypatch):
    """`trend_profile.build` 一轮内被多次调用（走势 + 情景 + 区间）→ 同 key 只查一次。

    假 engine 对任何 symbol 都返回同一份 2 行数据，所以 build 的兜底链会依次试
    000300 / 000001（各 1 次）；两次 build 共 3 次查询，而不是 6 次。
    """
    from src.core import trend_profile

    fake = _FakeEngine()
    monkeypatch.setattr("src.db.session.engine", fake)

    token = bc.begin_bar_scope()
    try:
        trend_profile.build("600111", "CN")
        trend_profile.build("600111", "CN")
    finally:
        bc.end_bar_scope(token)
    assert fake.queries == 3, f"作用域内两次 build 打了 {fake.queries} 次库（期望 3）"

    fake2 = _FakeEngine()
    monkeypatch.setattr("src.db.session.engine", fake2)
    trend_profile.build("600111", "CN")
    trend_profile.build("600111", "CN")
    assert fake2.queries == 6, "出了作用域必须退回逐次直查（回填/回测不能吃缓存）"


def test_scenario_engine_uses_scope_cache(monkeypatch):
    """情景矩阵与走势共用同一份 250 日线 → 作用域内第二次不再查库。"""
    from src.core import scenario_engine

    fake = _FakeEngine()
    monkeypatch.setattr("src.db.session.engine", fake)

    token = bc.begin_bar_scope()
    try:
        scenario_engine.build_matrix(None, "600111", "CN")
        scenario_engine.build_matrix(None, "600111", "CN")
    finally:
        bc.end_bar_scope(token)
    assert fake.queries == 1, f"情景矩阵两次调用打了 {fake.queries} 次库（期望 1）"
