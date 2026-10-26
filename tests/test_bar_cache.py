"""单轮取数缓存（2026-10-23）：作用域内复用 / 作用域外直通 / 并发只查一次。

背景：一轮 deep_diagnosis 里 4 个工具各自调 `load_qfq_bars(symbol, market, 250)`，
同一条 SQL 打 4 次 PG。日线在一轮问句内不可能变 —— 缓存只在这一轮内有效，
出了作用域必须逐字退回"每次直查"，否则回填/因子/回测会读到陈旧 K 线。

所以这里守两件事：**命中率**（作用域内只查一次）和**不出作用域**（无作用域 == 不缓存）。
"""
import asyncio
from datetime import datetime

import pytest

import src.harness.bar_cache as bc
from src.db import klines_repo


def _bars(n: int = 3, tag: str = "b") -> list[dict]:
    return [
        {"date": f"2026-10-{10 + i:02d}", "open": 1.0 + i, "high": 1.5 + i,
         "low": 0.9 + i, "close": 1.2 + i, "volume": 1000.0 + i, "tag": tag}
        for i in range(n)
    ]


class Loader:
    """同步假 loader：记录调用次数，可返回固定 bars 或抛异常。"""

    def __init__(self, bars=None, fail: Exception | None = None):
        self.calls: list[tuple] = []
        self.bars = _bars() if bars is None else bars
        self.fail = fail

    def __call__(self, symbol, market, days):
        self.calls.append((symbol, market, days))
        if self.fail is not None:
            raise self.fail
        return [dict(b) for b in self.bars]          # 每次返回新对象，能验证缓存不是靠同一份引用


class AsyncLoader:
    """异步假 loader：sleep 一下，制造真正的并发窗口（同步 loader 不会被抢占，测不出 single-flight）。"""

    def __init__(self, bars=None):
        self.calls: list[tuple] = []
        self.bars = _bars() if bars is None else bars

    async def __call__(self, symbol, market, days):
        self.calls.append((symbol, market, days))
        await asyncio.sleep(0.01)
        return [dict(b) for b in self.bars]


# ── 1. 无作用域 = 不缓存（最重要的一条） ────────────────────────────────


def test_passthrough_without_scope_always_calls_loader():
    """没有作用域时必须逐字直通：回填/因子/回测不能读到缓存。"""
    ld = Loader()
    assert bc.scope_active() is False
    a = bc.cached_load_bars("002050", "CN", 250, ld)
    b = bc.cached_load_bars("002050", "CN", 250, ld)
    assert len(ld.calls) == 2, "无作用域还命中缓存 = 非 chat 调用方会读到陈旧 K 线"
    assert a == b == ld.bars


def test_end_scope_restores_passthrough():
    """作用域关掉以后，同样的调用必须重新打 loader。"""
    ld = Loader()
    token = bc.begin_bar_scope()
    try:
        bc.cached_load_bars("002050", "CN", 250, ld)
        bc.cached_load_bars("002050", "CN", 250, ld)
        assert len(ld.calls) == 1
    finally:
        bc.end_bar_scope(token)
    assert bc.scope_active() is False
    bc.cached_load_bars("002050", "CN", 250, ld)
    assert len(ld.calls) == 2


def test_end_scope_tolerates_bad_token():
    """token 无效/跨 context 复位时必须静默，不能把管线带崩。"""
    bc.end_bar_scope(None)
    bc.end_bar_scope(object())          # 非 Token：复位抛 ValueError → 内部吞掉


# ── 2. 作用域内命中 ────────────────────────────────────────────────────


def test_scope_reuses_same_key():
    ld = Loader()
    token = bc.begin_bar_scope()
    try:
        first = bc.cached_load_bars("002050", "CN", 250, ld)
        second = bc.cached_load_bars("002050", "CN", 250, ld)
        third = bc.cached_load_bars("002050", "CN", 250, ld)
    finally:
        bc.end_bar_scope(token)
    assert len(ld.calls) == 1, f"同一 key 应该只查一次，实际 {ld.calls}"
    assert first == second == third
    assert first is not second, "每次返回浅拷贝，防止调用方就地排序污染缓存"


def test_distinct_keys_are_separate_entries():
    """days / market / symbol 任一不同都是另一条数据，不许互相顶替。"""
    ld = Loader()
    token = bc.begin_bar_scope()
    try:
        bc.cached_load_bars("002050", "CN", 250, ld)
        bc.cached_load_bars("002050", "CN", 120, ld)      # 不同 days
        bc.cached_load_bars("002050", "HK", 250, ld)      # 不同 market
        bc.cached_load_bars("600111", "CN", 250, ld)      # 不同 symbol
        assert len(ld.calls) == 4
        bc.cached_load_bars("002050", "cn", 250, ld)      # market 大小写归一 → 仍命中
        assert len(ld.calls) == 4
    finally:
        bc.end_bar_scope(token)


def test_market_enum_and_string_share_entry():
    """枚举 Market.CN 与字符串 'CN' 是同一只票，别因为类型不同多查一次。"""

    class M:
        value = "CN"

    ld = Loader()
    token = bc.begin_bar_scope()
    try:
        bc.cached_load_bars("002050", M(), 250, ld)
        bc.cached_load_bars("002050", "CN", 250, ld)
        assert len(ld.calls) == 1
    finally:
        bc.end_bar_scope(token)


# ── 3. TTL 与容量 ──────────────────────────────────────────────────────


def test_ttl_expiry_reloads(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(bc, "_now", lambda: clock["t"])
    ld = Loader()
    token = bc.begin_bar_scope(ttl=30)
    try:
        bc.cached_load_bars("002050", "CN", 250, ld)
        clock["t"] += 29
        bc.cached_load_bars("002050", "CN", 250, ld)
        assert len(ld.calls) == 1, "TTL 内必须命中"
        clock["t"] += 2                       # 31s > 30s
        bc.cached_load_bars("002050", "CN", 250, ld)
        assert len(ld.calls) == 2, "过期必须回源，否则跨轮会拿旧 K 线"
    finally:
        bc.end_bar_scope(token)


def test_entry_cap_evicts_oldest_fifo():
    """长轮次（全市场扫描）不能无界吃内存：超过上限按插入序淘汰最早的。"""
    ld = Loader()
    token = bc.begin_bar_scope(max_entries=2)
    try:
        for sym in ("000001", "000002", "000003"):
            bc.cached_load_bars(sym, "CN", 250, ld)
        scope = bc._SCOPE.get()
        assert len(scope.entries) == 2, f"容量没兜住: {list(scope.entries)}"
        assert ("000001", "CN", 250) not in scope.entries, "该淘汰最早写入的那条"

        bc.cached_load_bars("000003", "CN", 250, ld)     # 仍在缓存 → 不查库
        assert len(ld.calls) == 3
        bc.cached_load_bars("000001", "CN", 250, ld)     # 已被淘汰 → 回源
        assert len(ld.calls) == 4
    finally:
        bc.end_bar_scope(token)


def test_empty_result_is_not_cached():
    """`load_qfq_bars` 查库失败返回 []（不抛）。空结果不入缓存，否则第一次失败会变成整轮全空。"""
    ld = Loader(bars=[])
    token = bc.begin_bar_scope()
    try:
        assert bc.cached_load_bars("002050", "CN", 250, ld) == []
        assert bc.cached_load_bars("002050", "CN", 250, ld) == []
        assert len(ld.calls) == 2, "空结果必须当未命中，给下一次重试的机会"
    finally:
        bc.end_bar_scope(token)


# ── 4. 并发 single-flight ─────────────────────────────────────────────


def test_single_flight_concurrent_async_loads_one_query():
    """pipeline 用 asyncio.gather 并发跑工具：同 key 并发进来只能有一次查询。"""
    ld = AsyncLoader()

    async def one():
        return await bc.cached_load_bars_async("002050", "CN", 250, ld)

    async def go():
        token = bc.begin_bar_scope()
        try:
            return await asyncio.gather(one(), one(), one())
        finally:
            bc.end_bar_scope(token)

    results = asyncio.run(go())
    assert len(ld.calls) == 1, f"并发同 key 打了 {len(ld.calls)} 次库，single-flight 没生效"
    assert results[0] == results[1] == results[2] == ld.bars


def test_single_flight_supports_sync_loader():
    """同步 loader（真实调用方 klines_repo.load_qfq_bars）走 async 入口也不许重复查。"""
    ld = Loader()

    async def one():
        return await bc.cached_load_bars_async("002050", "CN", 250, ld)

    async def go():
        token = bc.begin_bar_scope()
        try:
            return await asyncio.gather(one(), one())
        finally:
            bc.end_bar_scope(token)

    asyncio.run(go())
    assert len(ld.calls) == 1


def test_async_entry_passthrough_without_scope():
    ld = AsyncLoader()

    async def go():
        return await asyncio.gather(
            bc.cached_load_bars_async("002050", "CN", 250, ld),
            bc.cached_load_bars_async("002050", "CN", 250, ld),
        )

    asyncio.run(go())
    assert len(ld.calls) == 2, "无作用域时异步入口也必须是直通"


# ── 5. 异常语义 ────────────────────────────────────────────────────────


def test_loader_exception_is_not_cached_and_next_call_retries():
    boom = RuntimeError("pg down")
    bad = Loader(fail=boom)
    good = Loader()
    token = bc.begin_bar_scope()
    try:
        with pytest.raises(RuntimeError):          # 缓存层不包装、不吞
            bc.cached_load_bars("002050", "CN", 250, bad)
        assert len(bad.calls) == 1
        out = bc.cached_load_bars("002050", "CN", 250, good)   # 下次还能重试
        assert out == good.bars and len(good.calls) == 1
    finally:
        bc.end_bar_scope(token)


def test_cache_bookkeeping_error_degrades_to_direct_load(monkeypatch):
    """缓存自身簿记炸了（这里让 _get 抛）也必须拿到数据 —— 缓存不许变成单点故障。"""
    ld = Loader()

    def boom(*_a, **_kw):
        raise ValueError("cache corrupt")

    monkeypatch.setattr(bc, "_get", boom)
    token = bc.begin_bar_scope()
    try:
        assert bc.cached_load_bars("002050", "CN", 250, ld) == ld.bars
        assert len(ld.calls) == 1
    finally:
        bc.end_bar_scope(token)


def test_begin_scope_never_raises_on_bad_params(monkeypatch):
    monkeypatch.setenv("HARNESS_BAR_CACHE_TTL", "not-a-number")
    token = bc.begin_bar_scope()
    try:
        assert bc.scope_active() is True, "非法 env 退回默认 TTL，不许整轮不缓存"
    finally:
        bc.end_bar_scope(token)


# ── 6. 生产入口：load_qfq_bars 原样保留，新增 load_qfq_bars_cached ──────


class _FakeEngine:
    """最小 SQLAlchemy engine 替身：只数 connect() 次数（= 打了几次 PG）。"""

    def __init__(self):
        self.queries = 0
        self.rows = [
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


def test_load_qfq_bars_is_not_wrapped(monkeypatch):
    """`load_qfq_bars` 必须保持"每次都是新查询"：回填/因子/回测直接用它。"""
    fake = _FakeEngine()
    monkeypatch.setattr("src.db.session.engine", fake)
    token = bc.begin_bar_scope()          # 哪怕在作用域里，原函数也绝不吃缓存
    try:
        klines_repo.load_qfq_bars("002050", "CN", 250)
        klines_repo.load_qfq_bars("002050", "CN", 250)
    finally:
        bc.end_bar_scope(token)
    assert fake.queries == 2, "load_qfq_bars 被缓存包住了 → 回填会读到旧数据"
    assert len(klines_repo.load_qfq_bars("002050", "CN", 250)) == 2


def test_load_qfq_bars_cached_hits_within_scope(monkeypatch):
    """作用域内走新入口：4 个工具 1 次查询（本任务的价值所在）。"""
    fake = _FakeEngine()
    monkeypatch.setattr("src.db.session.engine", fake)
    token = bc.begin_bar_scope()
    try:
        bars = [klines_repo.load_qfq_bars_cached("002050", "CN", 250) for _ in range(4)]
    finally:
        bc.end_bar_scope(token)
    assert fake.queries == 1, f"同一轮同一 key 打了 {fake.queries} 次库"
    assert all(b == bars[0] for b in bars) and bars[0][0]["date"] == "2026-10-19"


def test_load_qfq_bars_cached_passthrough_without_scope(monkeypatch):
    fake = _FakeEngine()
    monkeypatch.setattr("src.db.session.engine", fake)
    klines_repo.load_qfq_bars_cached("002050", "CN", 250)
    klines_repo.load_qfq_bars_cached("002050", "CN", 250)
    assert fake.queries == 2, "没有作用域还缓存 → 非 chat 调用方读到陈旧数据"


# ── 7. 管线接线：整轮开作用域，finally 必关 ─────────────────────────────


class _Recipe:
    id = "t_cache"
    tools = ("a", "b")
    output_sections = ("结论",)
    prompt_fragment = "你是测试员。"


def test_pipeline_run_opens_scope_for_whole_turn():
    """run() 内工具执行时作用域必须是开的（否则缓存一次都不会命中）。"""
    import src.harness.pipeline as pl

    seen: list[bool] = []

    async def ex(name, args):
        seen.append(bc.scope_active())
        return "data"

    async def go():
        runs, errs = await pl.run(_Recipe(), ["002050"], ex)
        return runs, errs, bc.scope_active()

    runs, errs, still_open = asyncio.run(go())
    assert seen and all(seen), "管线里工具执行时作用域没开"
    assert errs == [] and runs[0].ok == 2
    assert still_open is False, "run 结束必须复位作用域（下一轮要看新 K 线）"


def test_pipeline_run_closes_scope_even_when_tools_fail():
    import src.harness.pipeline as pl

    async def ex(name, args):
        raise RuntimeError("boom")

    async def go():
        await pl.run(_Recipe(), ["002050"], ex)
        return bc.scope_active()

    assert asyncio.run(go()) is False


def test_pipeline_run_stream_closes_scope():
    import src.harness.pipeline as pl

    async def ex(name, args):
        return "data"

    async def go():
        kinds = []
        async for kind, _payload in pl.run_stream(_Recipe(), ["002050"], ex):
            kinds.append(kind)
        return kinds, bc.scope_active()

    kinds, still_open = asyncio.run(go())
    assert kinds[-1] == "runs"
    assert still_open is False, "生成器结束必须复位作用域"
