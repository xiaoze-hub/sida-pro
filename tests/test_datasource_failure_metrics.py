"""数据源失败计数接线回归(2026-09-07 0.2)。

原状况: sida_datasource_failures_total 计数器全仓零调用方, prometheus-rules.yml
的 SidaDatasourceFailures 告警永不触发 —— 数据源全黑监控无声。

接线方式(与派单方案的偏差): 不在 31 个 vendor 文件里手写 .inc(), 也不让
packages/marketdata 反向 import src.web; 改为 vendors/base.Vendor.__init_subclass__
在 fetch 定义处自动包失败上报 + Engine 的 timeout/auth 分支补发事件,
health.py 导入时经 on_vendor_failure 注册桥接。一处覆盖直调/Engine/未来新增三条路径。

覆盖:
  - fetch 抛异常 → 监听者收到 (vendor.name, "fetch") 且异常原样上抛(不吞)
  - fetch 成功不误报; 监听者自身异常被隔离; 重复注册幂等
  - 桥接: record_datasource_failure 真实驱动 Prometheus 计数器
  - label 基数防线: labelnames 只有 provider/kind(无 symbol), kind 未知值归一 fetch
"""
from __future__ import annotations

import pytest

import marketdata.vendors.base as md_base
from marketdata.symbol import Market, Symbol
from marketdata.vendors.base import Vendor, emit_vendor_failure, on_vendor_failure


@pytest.fixture()
def clean_listeners():
    saved = list(md_base._FAILURE_LISTENERS)
    yield
    md_base._FAILURE_LISTENERS[:] = saved


def test_vendor_fetch_failure_emits_and_reraises(clean_listeners):
    """fetch 抛异常时: 监听者收到 (name, "fetch"), 且异常原样上抛(不吞)。"""
    events = []
    on_vendor_failure(lambda p, k="fetch": events.append((p, k)))

    class _BoomVendor(Vendor):
        name = "boom-test"

        def fetch(self, symbols, config):
            raise RuntimeError("网络炸了")

    with pytest.raises(RuntimeError):
        _BoomVendor().fetch([], {})
    assert events == [("boom-test", "fetch")]


def test_vendor_fetch_success_no_emit(clean_listeners):
    """fetch 成功不误报。"""
    events = []
    on_vendor_failure(lambda p, k="fetch": events.append((p, k)))

    class _OkVendor(Vendor):
        name = "ok-test"

        def fetch(self, symbols, config):
            return ["row"]

    assert _OkVendor().fetch([], {}) == ["row"]
    assert events == []


def test_emit_listener_exception_isolated(clean_listeners):
    """某个监听者自身抛异常不影响其他监听者, 也不外泄。"""
    called = []
    on_vendor_failure(lambda p, k="fetch": (_ for _ in ()).throw(RuntimeError("监听者坏")))
    on_vendor_failure(lambda p, k="fetch": called.append(p))
    emit_vendor_failure("prov-x", "timeout")
    assert called == ["prov-x"]


def test_on_vendor_failure_idempotent(clean_listeners):
    """同一监听者重复注册只生效一次。"""
    calls = []

    def _listener(p, k="fetch"):
        calls.append(p)

    on_vendor_failure(_listener)
    on_vendor_failure(_listener)
    emit_vendor_failure("prov-y")
    assert calls == ["prov-y"]


def test_real_vendor_instrumented(clean_listeners, monkeypatch):
    """真实 vendor(tencent)取数失败也走同一包装: 计数 + 原样抛出。"""
    from marketdata.vendors.tencent import TencentQuoteVendor

    events = []
    on_vendor_failure(lambda p, k="fetch": events.append((p, k)))

    def _network_down(*a, **kw):
        raise ConnectionError("拔网线")

    monkeypatch.setattr("marketdata.vendors.tencent.market_get", _network_down)
    v = TencentQuoteVendor()
    with pytest.raises(ConnectionError):
        v.fetch([Symbol(Market.CN, "600000")], {})
    assert events and events[0][0] == v.name and events[0][1] == "fetch"


def _counter():
    from src.web.api import health as health_mod

    health_mod._init_metrics()
    if health_mod._metrics.DATASOURCE_FAILURES is None:
        pytest.skip("prometheus_client 不可用")
    return health_mod


def test_bridge_increments_prometheus_counter():
    """桥接真实驱动 sida_datasource_failures_total, 且 labelnames 只有 provider/kind。"""
    health_mod = _counter()
    from prometheus_client import REGISTRY

    before = REGISTRY.get_sample_value(
        "sida_datasource_failures_total", {"provider": "probe-bridge", "kind": "timeout"}
    ) or 0.0
    health_mod.record_datasource_failure("probe-bridge", kind="timeout")
    after = REGISTRY.get_sample_value(
        "sida_datasource_failures_total", {"provider": "probe-bridge", "kind": "timeout"}
    )
    assert after == before + 1
    assert set(health_mod._metrics.DATASOURCE_FAILURES._labelnames) == {"provider", "kind"}
    health_mod._metrics.DATASOURCE_FAILURES.remove("probe-bridge", "timeout")


def test_kind_normalization_unknown_to_fetch():
    """kind 传进未定义值时归一为 fetch, 防 label 基数膨胀。"""
    health_mod = _counter()
    from prometheus_client import REGISTRY

    health_mod.record_datasource_failure("probe-norm", kind="not-a-kind")
    assert REGISTRY.get_sample_value(
        "sida_datasource_failures_total", {"provider": "probe-norm", "kind": "fetch"}
    ) == 1.0
    health_mod._metrics.DATASOURCE_FAILURES.remove("probe-norm", "fetch")
