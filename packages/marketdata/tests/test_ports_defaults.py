from marketdata.defaults import InMemoryMetricsSink, StaticConfigProvider
from marketdata.ports import SourceConfig


def test_static_config_returns_sorted_enabled():
    cp = StaticConfigProvider({
        "quote": [
            SourceConfig(vendor="b", priority=2),
            SourceConfig(vendor="a", priority=1),
            SourceConfig(vendor="x", priority=0, enabled=False),
        ]
    })
    got = [s.vendor for s in cp.sources_for("quote", "CN")]
    assert got == ["a", "b"]  # 已按 priority 排序、禁用的被剔除


def test_static_config_unknown_type_empty():
    cp = StaticConfigProvider({})
    assert cp.sources_for("kline", "CN") == []


def test_metrics_snapshot():
    m = InMemoryMetricsSink()
    m.record(vendor="a", datatype="quote", market="CN", ok=True, count=3, latency_ms=100)
    m.record(vendor="a", datatype="quote", market="CN", ok=False, count=0, latency_ms=200, error="boom")
    snap = m.snapshot()["a"]
    assert snap["count"] == 2
    assert snap["success_rate"] == 0.5
    assert snap["last_error"] == "boom"


def test_metrics_ewma_latency():
    """延迟 EWMA(C1): 首笔=样值, 之后 alpha=0.3 递推; 失败样本也计入(超时体现为高延迟)。"""
    m = InMemoryMetricsSink()
    m.record(vendor="a", datatype="quote", market="CN", ok=True, count=1, latency_ms=1000)
    assert m.snapshot()["a"]["ewma_latency_ms"] == 1000
    m.record(vendor="a", datatype="quote", market="CN", ok=True, count=1, latency_ms=100)
    assert m.snapshot()["a"]["ewma_latency_ms"] == 730  # 0.3*100 + 0.7*1000
    m.record(vendor="a", datatype="quote", market="CN", ok=False, count=0, latency_ms=8000, error="timeout")
    assert m.snapshot()["a"]["ewma_latency_ms"] == 2911  # 0.3*8000 + 0.7*730


def test_metrics_ewma_none_before_first_record():
    """从未记录过 → ewma=None(不冒充 0/不编造)。"""
    m = InMemoryMetricsSink()
    assert m.snapshot() == {}
    m.record(vendor="b", datatype="quote", market="CN", ok=True, count=1, latency_ms=50)
    assert m.snapshot()["b"]["ewma_latency_ms"] == 50
