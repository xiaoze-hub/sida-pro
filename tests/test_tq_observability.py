"""TQ 监控与降级可见性回归(2026-10-08 可观测性审计)。

覆盖 P1-6/P1-7 + 4 项 P2:
  · 直连 tq_rpc 失败进失败指标(观测包装, provider='tq', kind=调用名)
  · 哨兵 tq_gateway 检查: 连续失败/持续 degraded → 告警通知
  · 失败日志限流(同 key 60s 内只一条 + 累计计数)
  · 涨停池各源统一写 source 并透传展示
  · 暗盘北交所代码映射(复用 to_tq_code, 不再静默 None)
  · 妖股因子 TQ 断链显式错误态(failed/ok)
不触网: mock vendor/rpc/health。
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ────────────────────────── P1-6: 直连 tq_rpc 失败上报 ──────────────────────────


@pytest.fixture()
def clean_listeners():
    import marketdata.vendors.base as md_base

    saved = list(md_base._FAILURE_LISTENERS)
    yield
    md_base._FAILURE_LISTENERS[:] = saved


def test_tq_rpc_guard_emits_method_kind_and_reraises(clean_listeners):
    import marketdata.vendors.tq as tqmod
    from marketdata.vendors.base import on_vendor_failure
    from src.core import tq_rpc_observability as obs

    events = []
    on_vendor_failure(lambda p, k="fetch": events.append((p, k)))
    assert obs.install_tq_rpc_guard() in (True, False)  # 幂等, 已装也 OK
    assert obs.is_installed()

    def boom(method, params, timeout=None):
        raise ConnectionError("TQ 断链")

    orig = getattr(tqmod, "_tq_rpc_orig", None)
    tqmod._tq_rpc_orig = boom
    try:
        with pytest.raises(ConnectionError):
            tqmod.tq_rpc("get_market_data", {"stock_list": []})
    finally:
        tqmod._tq_rpc_orig = orig

    assert ("tq", "get_market_data") in events


def test_install_is_idempotent():
    from src.core import tq_rpc_observability as obs

    obs.install_tq_rpc_guard()
    assert obs.install_tq_rpc_guard() is False  # 第二次不重装


def test_tq_kind_whitelist_bounds_cardinality():
    from src.core.tq_rpc_observability import tq_kind

    assert tq_kind("get_more_info") == "get_more_info"
    assert tq_kind("some_random_method") == "fetch"  # 白名单外归一, 防基数膨胀
    assert tq_kind("") == "fetch"


def test_bridge_counts_method_kind():
    from src.web.api import health as health_mod

    health_mod._init_metrics()
    if health_mod._metrics.DATASOURCE_FAILURES is None:
        pytest.skip("prometheus_client 不可用")
    # delta 断言: 进程级 Prometheus 计数器会被前序测试累积, 绝对值断言与执行顺序耦合
    # (单跑绿/合跑红); 语义不变 —— 一次调用恰好 +1, 顺序无关
    from prometheus_client import REGISTRY

    _labels = {"provider": "tq", "kind": "get_market_data"}
    before = REGISTRY.get_sample_value("sida_datasource_failures_total", _labels)
    health_mod.record_datasource_failure("tq", kind="get_market_data")
    val = REGISTRY.get_sample_value("sida_datasource_failures_total", _labels)
    assert val == (before or 0.0) + 1.0
    health_mod._metrics.DATASOURCE_FAILURES.remove("tq", "get_market_data")


# ────────────────────────── P2: 失败日志限流 ──────────────────────────


def test_log_throttle_one_per_window_then_counts():
    from marketdata.log_throttle import reset, should_log

    reset()
    assert should_log("tq:fetch", now=1000.0) == (True, 0)
    assert should_log("tq:fetch", now=1010.0) == (False, 1)
    assert should_log("tq:fetch", now=1020.0) == (False, 2)
    # 跨窗口重置, 重新放行
    assert should_log("tq:fetch", now=1070.0) == (True, 0)


def test_log_failure_suppresses_and_reports_count(caplog):
    import logging

    from marketdata.log_throttle import log_failure, reset

    reset()
    logger = logging.getLogger("test.log_throttle.emit")
    with caplog.at_level(logging.WARNING, logger="test.log_throttle.emit"):
        assert log_failure(logger, "tq:get_market_data", "boom") is True
        assert log_failure(logger, "tq:get_market_data", "boom") is False
    assert len([r for r in caplog.records if r.name == "test.log_throttle.emit"]) == 1


def test_engine_timeout_log_is_throttled(caplog):
    """Engine 同一 vendor 连续超时 60s 内只打一条 warning。"""
    import logging
    from types import SimpleNamespace

    from marketdata.cache import TTLCache
    from marketdata.engine import Engine
    from marketdata.log_throttle import reset
    from marketdata.symbol import Symbol  # noqa: F401
    from marketdata.types import Request
    from marketdata.vendors.base import Vendor

    reset()

    class _TimeoutVendor(Vendor):
        name = "tqv"

        def fetch(self, symbols, config):
            raise TimeoutError("slow")

    eng = Engine(
        datatype="quote",
        vendors={"tqv": _TimeoutVendor()},
        config=SimpleNamespace(sources_for=lambda dt, mk: [
            SimpleNamespace(vendor="tqv", priority=1, enabled=True, config={}, key_pool=[]),
        ]),
        metrics=SimpleNamespace(record=lambda **kw: None),
        cache=TTLCache(),
        default_ttl=0.0,
    )
    with caplog.at_level(logging.WARNING, logger="marketdata.engine"):
        eng.fetch(Request(market="CN", symbols=["600519"]))
        eng.fetch(Request(market="CN", symbols=["600519"]))
    # 同一 (vendor, kind) 连续失败: 只有一条 warning(TIMEOUT/raised 分别键, 这里是 raised:fetch)
    msgs = [r for r in caplog.records if r.name == "marketdata.engine"]
    assert len(msgs) == 1, [m.getMessage() for m in msgs]


# ────────────────────────── P1-7: 哨兵 TQ 网关检查 ──────────────────────────


@pytest.fixture()
def dq_db():
    from src.web.database import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture()
def _reset_tq_streak():
    from src.core import data_quality_sentinel as dq

    dq._tq_degraded_streak = 0
    yield
    dq._tq_degraded_streak = 0


def _stub_source_status(monkeypatch, status, detail=""):
    import src.core.source_health as sh

    monkeypatch.setattr(
        sh, "check_source",
        lambda sid, use_cache=True: {"id": sid, "status": status, "detail": detail},
    )


def test_tq_gateway_ok_by_default(dq_db, _reset_tq_streak, monkeypatch):
    from src.core import alerting
    from src.core.data_quality_sentinel import _check_tq_gateway

    monkeypatch.setattr(alerting, "failure_streak", lambda p: 0)
    _stub_source_status(monkeypatch, "degraded", "未配置 TDX_QUANT_URL 且尚未自动发现")
    r = _check_tq_gateway(dq_db, datetime(2026, 10, 8, 10, 0, 0))
    assert r["status"] == "ok"
    assert r["value"]["streak"] == 0


def test_tq_gateway_warns_on_any_streak(dq_db, _reset_tq_streak, monkeypatch):
    from src.core import alerting
    from src.core.data_quality_sentinel import _check_tq_gateway

    monkeypatch.setattr(alerting, "failure_streak", lambda p: 1)
    _stub_source_status(monkeypatch, "degraded", "未配置")
    r = _check_tq_gateway(dq_db, datetime(2026, 10, 8, 10, 0, 0))
    assert r["status"] == "warn"


def test_tq_gateway_fails_on_streak_threshold(dq_db, _reset_tq_streak, monkeypatch):
    from src.core import alerting
    from src.core.data_quality_sentinel import _check_tq_gateway

    monkeypatch.setattr(alerting, "failure_streak", lambda p: 3)
    _stub_source_status(monkeypatch, "degraded", "TQ 网关当前不通: x")
    r = _check_tq_gateway(dq_db, datetime(2026, 10, 8, 10, 0, 0))
    assert r["status"] == "fail"


def test_tq_gateway_escalates_on_continued_degraded(dq_db, _reset_tq_streak, monkeypatch):
    """地址已发现但当前不通, 连续 3 轮 → fail(即使失败计数为 0)。"""
    from src.core import alerting
    from src.core.data_quality_sentinel import _check_tq_gateway

    monkeypatch.setattr(alerting, "failure_streak", lambda p: 0)
    _stub_source_status(monkeypatch, "degraded", "TQ 网关当前不通: http://x/")
    r = None
    for _ in range(3):
        r = _check_tq_gateway(dq_db, datetime(2026, 10, 8, 10, 0, 0))
    assert r["status"] == "fail"
    assert r["value"]["degraded_streak"] >= 3


def test_tq_gateway_triggers_notification(dq_db, _reset_tq_streak, monkeypatch):
    """哨兵整体跑一轮: TQ 连续失败 → 写通知(push_notification)。"""
    from src.core import alerting
    from src.core.data_quality_sentinel import run_dq_checks

    monkeypatch.setattr(alerting, "failure_streak", lambda p: 5)
    _stub_source_status(monkeypatch, "degraded", "TQ 网关当前不通")
    pushed = []
    monkeypatch.setattr(
        "src.core.notify_center.push_notification",
        lambda **kw: pushed.append(kw) or 1,
    )
    res = run_dq_checks(dq_db)
    assert res["overall"] == "fail"
    assert any(c["check"] == "tq_gateway" and c["status"] == "fail" for c in res["checks"])
    assert pushed and pushed[0]["level"] == "error"


# ────────────────────────── P2: 涨停池来源标注 ──────────────────────────


def test_wudao_pool_rows_carry_source(monkeypatch):
    from src.collectors import market_sentiment_collector as msc

    class _FakeClient:
        def call_tool(self, name, params):
            return {"rows": [
                {"code": "001234", "name": "泰慕士", "continueNum": 3, "industry": "纺织"},
            ]}

    monkeypatch.setattr("src.collectors.wudao_mcp_client.WudaoMCPClient", _FakeClient)
    rows = msc.MarketSentimentCollector()._limit_up_pool_wudao("20261008")
    assert rows and all(r["source"] == "wudao" for r in rows)


def test_sentiment_summary_propagates_source(monkeypatch):
    from src.collectors.market_sentiment_collector import MarketSentimentCollector

    pool = [
        {"code": "001234", "name": "A", "price": 1.0, "pct": 10.0, "amount": 1.0, "ltsz": 1.0,
         "first_time": "09:30:00", "last_time": "", "days": 2, "sector": "纺织", "theme": "",
         "reason": "", "turnover_rate": 0.0, "order_amount": 0.0, "source": "tq_zdt"},
    ]
    monkeypatch.setattr(MarketSentimentCollector, "get_limit_up_pool", lambda self, date=None: pool)
    s = MarketSentimentCollector().get_sentiment_summary()
    assert s["limit_up_source"] == "tq_zdt"
    assert s["limit_up_sources"] == {"tq_zdt": 1}
    assert s["candidates"][0]["source"] == "tq_zdt"


def test_all_pool_sources_are_labelled():
    """四级降级链的四种 source 都在口径内, 消费方可追溯。"""
    from src.core.limit_up_calc import build_pool_items

    row = build_pool_items({"001234.SZ": {"Close": ["5.0", "5.5"]}})[0]
    assert row["source"] == "tq"
    from src.collectors.market_sentiment_collector import _parse_ztpool

    rows, _ = _parse_ztpool({"data": {"tc": 1, "pool": [{"c": "001234", "n": "x", "lbc": 1}]}})
    assert rows[0]["source"] == "eastmoney"


# ────────────────────────── P2: 暗盘北交所代码映射 ──────────────────────────


@pytest.mark.parametrize("code,expected", [
    ("600519", "600519.SH"),
    ("688318", "688318.SH"),
    ("000001", "000001.SZ"),
    ("300750", "300750.SZ"),
    ("430047", "430047.BJ"),
    ("831010", "831010.BJ"),
    ("870204", "870204.BJ"),
    ("920001", "920001.BJ"),
])
def test_dark_fund_to_tq_code(code, expected):
    from src.core.tdx_dark_fund import _to_tq_code

    assert _to_tq_code(code) == expected


def test_dark_fund_to_tq_code_invalid_returns_none():
    from src.core.tdx_dark_fund import _to_tq_code

    assert _to_tq_code("") is None
    assert _to_tq_code("abc") is None
    assert _to_tq_code("600519.SH") is None  # 非 6 位裸码


# ────────────────────────── P2: 妖股因子错误态 ──────────────────────────


def test_backfill_incremental_reports_failure(monkeypatch):
    from src.core import demon_factors as df

    monkeypatch.setattr(df, "_existing_max_dates", lambda: {})

    def _boom(symbol, days, *, raise_on_error=False):
        raise ConnectionError("TQ 全断")

    monkeypatch.setattr(df, "_tq_bars_direct", _boom)
    r = df.backfill_incremental(["600519", "000001"])
    assert r["failed"] == 2
    assert r["ok"] is False
    assert r["events_saved"] == 0


def test_update_pipeline_ok_false_when_tq_down(monkeypatch):
    from src.core import demon_factors as df

    monkeypatch.setattr(df, "_stock_names", lambda: {"600519": "贵州茅台"})
    monkeypatch.setattr(df, "_existing_max_dates", lambda: {})

    def _boom(symbol, days, *, raise_on_error=False):
        raise ConnectionError("TQ 全断")

    monkeypatch.setattr(df, "_tq_bars_direct", _boom)
    monkeypatch.setattr(df, "recompute_factors", lambda symbols=None: {"factor_date": "20261008", "updated": 0})
    out = df.update_pipeline()
    assert out["ok"] is False
    assert out["incremental"]["failed"] == 1
