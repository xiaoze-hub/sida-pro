from marketdata.cache import TTLCache
from marketdata.defaults import InMemoryMetricsSink
from marketdata.engine import Engine
from marketdata.ports import SourceConfig
from marketdata.types import Request


class FakeVendor:
    def __init__(self, name, behavior):
        self.name, self.behavior, self.supports_markets = name, behavior, set()

    def fetch(self, symbols, config):
        if self.behavior == "raise":
            raise RuntimeError("boom")
        if self.behavior == "empty":
            return []
        return [{"symbol": symbols[0].code, "v": self.name}]


class FakeConfig:
    def __init__(self, srcs):
        self._srcs = srcs

    def sources_for(self, datatype, market):
        return list(self._srcs)


def _engine(vendors, srcs, metrics=None):
    return Engine(
        datatype="quote", vendors=vendors, config=FakeConfig(srcs),
        metrics=metrics or InMemoryMetricsSink(), cache=TTLCache(5.0), default_ttl=5.0,
    )


def _req():
    return Request(symbols=("600519",), market="CN")


def test_first_success_wins():
    e = _engine({"a": FakeVendor("a", "ok"), "b": FakeVendor("b", "ok")},
                [SourceConfig(vendor="a", priority=1), SourceConfig(vendor="b", priority=2)])
    r = e.fetch(_req())
    assert r.ok and r.vendor == "a" and r.data[0]["v"] == "a"


def test_failover_empty_then_raise_then_ok():
    e = _engine({"a": FakeVendor("a", "empty"), "b": FakeVendor("b", "raise"), "c": FakeVendor("c", "ok")},
                [SourceConfig(vendor="a", priority=1), SourceConfig(vendor="b", priority=2), SourceConfig(vendor="c", priority=3)])
    r = e.fetch(_req())
    assert r.ok and r.vendor == "c"


def test_all_fail_returns_not_ok():
    e = _engine({"a": FakeVendor("a", "empty")}, [SourceConfig(vendor="a", priority=1)])
    assert e.fetch(_req()).ok is False


def test_market_filter_skips_unsupported():
    v = FakeVendor("a", "ok")
    v.supports_markets = {"US"}   # 不支持 CN
    assert _engine({"a": v}, [SourceConfig(vendor="a", priority=1)]).fetch(_req()).ok is False


def test_cache_hit_skips_second_call():
    v = FakeVendor("a", "ok")
    calls = {"n": 0}
    inner = v.fetch
    v.fetch = lambda s, c: (calls.__setitem__("n", calls["n"] + 1), inner(s, c))[1]
    e = _engine({"a": v}, [SourceConfig(vendor="a", priority=1)])
    e.fetch(_req()); e.fetch(_req())
    assert calls["n"] == 1


def test_metrics_recorded():
    m = InMemoryMetricsSink()
    _engine({"a": FakeVendor("a", "ok")}, [SourceConfig(vendor="a", priority=1)], metrics=m).fetch(_req())
    assert m.snapshot()["a"]["success_rate"] == 1.0


def test_priority_resort_when_config_unsorted():
    e = _engine({"a": FakeVendor("a", "ok"), "b": FakeVendor("b", "ok")},
                [SourceConfig(vendor="b", priority=2), SourceConfig(vendor="a", priority=1)])
    r = e.fetch(_req())
    assert r.ok and r.vendor == "a"


def test_min_count_prefers_first_sufficient():
    e = _engine({"a": FakeVendor("a", "ok"), "b": FakeVendor("b", "ok")},
                [SourceConfig(vendor="a", priority=1), SourceConfig(vendor="b", priority=2)])
    # FakeVendor "ok" 返回 1 条;min_count=2 → a 不足 → 试 b → b 也 1 条 → 都不足 → 取最长(并列取先到的 a)
    r = e.fetch(Request(symbols=("600519",), market="CN"), min_count=2)
    assert r.ok and len(r.data) == 1  # 返回了(最长的),不因不足而失败


def test_min_count_returns_first_meeting_threshold():
    class MultiVendor:
        def __init__(self, name, n): self.name=name; self.supports_markets=set(); self._n=n
        def fetch(self, symbols, config): return [{"i": i} for i in range(self._n)]
    e = _engine({"a": MultiVendor("a", 1), "b": MultiVendor("b", 5)},
                [SourceConfig(vendor="a", priority=1), SourceConfig(vendor="b", priority=2)])
    r = e.fetch(Request(symbols=("x",), market="CN"), min_count=3)
    assert r.ok and r.vendor == "b" and len(r.data) == 5  # a 不足(1<3)→ b 足(5≥3)


def test_min_count_default_one_unchanged():
    e = _engine({"a": FakeVendor("a", "ok")}, [SourceConfig(vendor="a", priority=1)])
    r = e.fetch(Request(symbols=("x",), market="CN"))  # 默认 min_count=1
    assert r.ok and r.vendor == "a"


def test_engine_passes_request_limit_as_days_to_vendor():
    seen = {}
    class DaysVendor:
        name = "d"
        supports_markets: set = set()
        def fetch(self, symbols, config):
            seen["days"] = config.get("days")
            return [{"x": 1}]
    e = _engine({"d": DaysVendor()}, [SourceConfig(vendor="d", priority=1)])
    e.fetch(Request(symbols=("x",), market="CN", limit=250))
    assert seen["days"] == 250


def test_engine_passes_request_extra_to_vendor_config():
    # 守护测试:events 等 vendor 需要 req.extra(如 since_days)透传进 call_config。
    seen = {}
    class ExtraVendor:
        name = "e"
        supports_markets: set = set()
        def fetch(self, symbols, config):
            seen["since_days"] = config.get("since_days")
            seen["days"] = config.get("days")
            return [{"x": 1}]
    e = _engine({"e": ExtraVendor()}, [SourceConfig(vendor="e", priority=1)])
    e.fetch(Request(symbols=("x",), market="CN", limit=99, extra=(("since_days", 30),)))
    assert seen["since_days"] == 30
    assert seen["days"] == 99  # extra 透传不应破坏原有 days 注入


class _BatchVendor:
    """按 symbols 顺序返回前 n 只（模拟"这一路只补到部分标的"）。"""

    def __init__(self, name, n):
        self.name, self._n, self.supports_markets = name, n, set()
        self.calls = 0

    def fetch(self, symbols, config):
        self.calls += 1
        return [{"symbol": s.code, "v": self.name} for s in symbols[: self._n]]


def test_partial_batch_falls_back_to_whole_batch_complete_source():
    """部分批次**不再**冒充完整：引擎按整批回退到能覆盖全部标的的源。

    2026-10-29 复审复现的缺陷：TQ 中途传输故障只回来 3/5，默认 min_count=1 ⇒
    这份部分结果被当成功缓存/返回，腾讯那一批快源**根本没被问过** ⇒ 缺失票在
    TTL 内一直没有行情，而调用方只知道"有 3 条"，不知道有 2 只被丢了。

    新口径（整批、不逐票、不混源）：
      - 某源覆盖不全 ⇒ 记为 partial 候选，继续问下一个源（整批）；
      - 下一个源能覆盖全部 ⇒ **整批**返回它（vendor 字段如实标成它，不拼两家的票）；
      - 所有源都不全 ⇒ 返回覆盖最广的那份，但 `partial=True` + `missing` 列明缺谁。
    """
    a, b = _BatchVendor("a", 1), _BatchVendor("b", 2)
    srcs = [SourceConfig(vendor="a", priority=1), SourceConfig(vendor="b", priority=2)]
    req = Request(symbols=("600519", "002827"), market="CN")

    r = _engine({"a": a, "b": b}, srcs).fetch(req)  # 默认 min_count=1
    assert r.ok and r.vendor == "b", "覆盖不全的源不该被当成最终答案"
    assert [d["symbol"] for d in r.data] == ["600519", "002827"]
    assert r.partial is False and r.missing == ()
    assert a.calls == 1 and b.calls == 1

    # min_count 的旧语义仍在：谁先满足阈值谁赢（都不满足时取覆盖最广的）
    r2 = _engine({"a": a, "b": b}, srcs).fetch(req, min_count=2)
    assert r2.ok and r2.vendor == "b" and len(r2.data) == 2


def test_partial_batch_is_flagged_and_never_merged_when_no_source_is_complete():
    """没有源能覆盖全部标的时：返回一份 partial 结果，**不合并**两家的票。"""
    a, b = _BatchVendor("a", 1), _BatchVendor("b", 1)
    srcs = [SourceConfig(vendor="a", priority=1), SourceConfig(vendor="b", priority=2)]
    req = Request(symbols=("600519", "002827"), market="CN")

    r = _engine({"a": a, "b": b}, srcs).fetch(req)
    assert r.ok and r.partial is True
    assert r.requested == 2
    assert r.missing == ("002827",), "缺哪只必须显式列出来"
    assert [d["symbol"] for d in r.data] == ["600519"], "绝不把 b 的 002827 拼进 a 的结果（混源假完整）"
    assert r.vendor == "a"


def test_partial_batch_is_not_cached():
    """partial 不进缓存：缓存它等于让缺失标的在整个 TTL 内都没有行情。"""
    v = _BatchVendor("a", 1)
    e = _engine({"a": v}, [SourceConfig(vendor="a", priority=1)])
    req = Request(symbols=("600519", "002827"), market="CN")

    r1 = e.fetch(req)
    r2 = e.fetch(req)
    assert r1.partial and r2.partial
    assert v.calls == 2, "partial 结果被缓存 ⇒ 第二次连源都不问了"


def test_partial_batch_is_recorded_as_incomplete_in_metrics():
    """指标里 partial 不能记成完整成功（否则健康面板把缺票的批次算 100% 成功）。"""
    class _Sink:
        def __init__(self):
            self.rows = []

        def record(self, **kw):
            self.rows.append(kw)

    sink = _Sink()
    v = _BatchVendor("a", 1)
    _engine({"a": v}, [SourceConfig(vendor="a", priority=1)], metrics=sink).fetch(
        Request(symbols=("600519", "002827"), market="CN")
    )
    assert len(sink.rows) == 1
    assert sink.rows[0]["ok"] is False
    assert "partial" in sink.rows[0]["error"]
    assert sink.rows[0]["count"] == 1
