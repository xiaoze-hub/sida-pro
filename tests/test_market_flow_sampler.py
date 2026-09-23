"""大盘资金流快照采样器测试(2026-09-11): 时段守卫 / 取数失败不抛 / 成功落库。

背景: 快照原先只在前端调用大盘资金流接口时写入 → 页面不开则日内曲线空白("没内容")。
"""
from __future__ import annotations

import pytest

import src.db.session as dbs
import src.core.market_flow_sampler as sampler
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from src.web.migrations import _m138_market_flow_snapshots_table


def _mk_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m138_market_flow_snapshots_table(conn)
    return eng


def _patch_db(monkeypatch):
    eng = _mk_engine()
    monkeypatch.setattr(dbs, "engine", eng)
    return eng


def _count(eng) -> int:
    with eng.begin() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM market_flow_snapshots")).scalar() or 0)


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


@pytest.fixture
def _no_tq(monkeypatch):
    """把 TQ 涨跌家数源打桩成不可用。

    2026-09-23 清单切换后, 采样器会去问 TQ 要涨跌家数。**不打桩的话测试结果取决于
    运行环境能否连到 TQ 网关**(CI 连不上 → 走网关值; 本机连着 → 被 TQ 覆盖),
    那就成了环境依赖的脆弱测试。下面测"网关路径"的用例统一打桩。
    """
    monkeypatch.setattr(sampler, "_tq_breadth_safe", lambda: None)


def test_skips_outside_trading_window(monkeypatch):
    eng = _patch_db(monkeypatch)
    import src.core.quote_snapshots as qs

    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: False)
    called = {"n": 0}

    def _req_get(*a, **k):
        called["n"] += 1
        raise AssertionError("非交易时段不应发请求")

    import requests

    monkeypatch.setattr(requests, "get", _req_get)
    out = sampler.collect_once()
    assert out["ok"] is True and out["skipped"] is True
    assert called["n"] == 0 and _count(eng) == 0


def test_fetch_failure_is_silent(monkeypatch, _no_tq):
    eng = _patch_db(monkeypatch)
    import src.core.quote_snapshots as qs

    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: True)
    import requests

    def _boom(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(requests, "get", _boom)
    out = sampler.collect_once()
    assert out["ok"] is False and "gateway down" in out["error"]
    assert _count(eng) == 0


def test_success_writes_snapshot_with_gateway_units(monkeypatch, _no_tq):
    eng = _patch_db(monkeypatch)
    import src.core.quote_snapshots as qs

    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: True)
    import requests

    payload = {
        "total_main_flow": -695.9,  # 亿
        "up_count": 333,
        "down_count": 4926,
        "flat_count": 25,
        "sh": {"main_flow": -325.3},
        "sz": {"main_flow": -370.6},
    }
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))
    out = sampler.collect_once()
    assert out["ok"] is True and out["skipped"] is False
    assert _count(eng) == 1
    with eng.begin() as conn:
        row = conn.execute(
            text("SELECT total_main_flow, up_count, down_count, sh_flow, sz_flow FROM market_flow_snapshots")
        ).fetchone()
    assert row[0] == -695.9 and row[1] == 333 and row[2] == 4926
    assert row[3] == -325.3 and row[4] == -370.6


# ═══════════ 清单切换: 涨跌家数走 TQ + 网关故障不再整段断 ═══════════
_TQ = {"up": 1891, "down": 3564, "flat": 121}


def test_tq_breadth_overrides_gateway_when_gateway_ok(monkeypatch):
    """网关正常时也用 TQ 的家数(换源去依赖), 资金字段仍走网关。"""
    eng = _patch_db(monkeypatch)
    import src.core.quote_snapshots as qs
    import requests

    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: True)
    monkeypatch.setattr(sampler, "_tq_breadth_safe", lambda: _TQ)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({
        "total_main_flow": -695.9, "up_count": 333, "down_count": 4926, "flat_count": 25,
        "sh": {"main_flow": -1.0}, "sz": {"main_flow": -2.0}}))
    out = sampler.collect_once()
    assert out["up_count"] == 1891 and out["flat_count"] == 121
    assert out["total_main_flow"] == -695.9      # 资金字段不受影响
    with eng.begin() as conn:
        r = conn.execute(text(
            "SELECT total_main_flow, up_count, down_count, flat_count FROM market_flow_snapshots")).fetchone()
    assert r[0] == -695.9 and r[1] == 1891 and r[2] == 3564 and r[3] == 121


def test_gateway_down_still_writes_breadth_only_row(monkeypatch):
    """回归锁: 网关挂时原实现直接 return, 一行都不落 → 家数曲线整段缺失。
    现在用 TQ 落一条只有家数的行, 资金字段留 NULL(不编造)。"""
    eng = _patch_db(monkeypatch)
    import src.core.quote_snapshots as qs
    import requests

    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: True)
    monkeypatch.setattr(sampler, "_tq_breadth_safe", lambda: _TQ)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({"error": "502 网关挂了"}))
    out = sampler.collect_once()
    assert out["ok"] is True and out.get("breadth_only") is True
    assert _count(eng) == 1
    with eng.begin() as conn:
        r = conn.execute(text(
            "SELECT total_main_flow, up_count, down_count, flat_count, sh_flow FROM market_flow_snapshots"
        )).fetchone()
    assert r[0] is None and r[4] is None            # 资金字段留空, 不编造
    assert (r[1], r[2], r[3]) == (1891, 3564, 121)


def test_gateway_request_exception_also_writes_breadth_row(monkeypatch):
    eng = _patch_db(monkeypatch)
    import src.core.quote_snapshots as qs
    import requests

    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: True)
    monkeypatch.setattr(sampler, "_tq_breadth_safe", lambda: _TQ)

    def _boom(*a, **k):
        raise RuntimeError("conn refused")

    monkeypatch.setattr(requests, "get", _boom)
    out = sampler.collect_once()
    assert out["ok"] is True and _count(eng) == 1


def test_gateway_down_and_no_tq_keeps_error(monkeypatch):
    """两个源都没有 → 保持原有错误语义, 不落假数据。"""
    eng = _patch_db(monkeypatch)
    import src.core.quote_snapshots as qs
    import requests

    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: True)
    monkeypatch.setattr(sampler, "_tq_breadth_safe", lambda: None)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({"error": "502"}))
    out = sampler.collect_once()
    assert out["ok"] is False and "502" in out["error"] and _count(eng) == 0


def test_gateway_partial_payload_with_tq_still_writes(monkeypatch):
    """网关通但缺 total_main_flow → 仍可落家数行(可空列已确认 YES)。"""
    eng = _patch_db(monkeypatch)
    import src.core.quote_snapshots as qs
    import requests

    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: True)
    monkeypatch.setattr(sampler, "_tq_breadth_safe", lambda: _TQ)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({"up_count": 1, "sh": {}, "sz": {}}))
    out = sampler.collect_once()
    assert out["ok"] is True and _count(eng) == 1 and out["total_main_flow"] is None


def test_gateway_partial_payload_without_tq_skips(monkeypatch):
    eng = _patch_db(monkeypatch)
    import src.core.quote_snapshots as qs
    import requests

    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: True)
    monkeypatch.setattr(sampler, "_tq_breadth_safe", lambda: None)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({"up_count": 1, "sh": {}, "sz": {}}))
    out = sampler.collect_once()
    assert out["ok"] is False and "total_main_flow" in out["error"] and _count(eng) == 0
