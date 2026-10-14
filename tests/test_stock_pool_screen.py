# -*- coding: utf-8 -*-
"""选股池共振扫描 并发化 + 缓存 + 基底降级 回归(2026-10-14)。

改造点(全 mock, **禁真网络**):
  ① `_screen` 串行 for → ThreadPoolExecutor: 50 只并发收敛(实测多线程 + 总耗时远小于串行);
  ② 结果走 biz_cache(60s): 二次调用**零外呼**;
  ③ 实时取数彻底失败 → 退 decision_cache **预落库基底**(口径不兼容字段如实置 None);
  ④ 单只彻底失败 → 显式"无"(source=none), 不阻塞整批。
"""
from __future__ import annotations

import threading
import time

import pytest

from src.web.api import stock_pool as sp


def _fake_pioneer(*, state="G区", signal="G", activity=8.0, level="大牛",
                  available=True, zjl=1e4, sleep=0.0):
    def _f(symbol, market="CN"):
        if sleep:
            time.sleep(sleep)
        return {
            "gs": {"state": state, "signal": signal, "pending": False},
            "institution_activity": {"activity": activity, "level": level},
            "l2": {"available": available, "zjl_hb": zjl},
            "main_intent": None,
        }

    return _f


@pytest.fixture(autouse=True)
def _clear_screen_cache():
    from src.web.cache.biz_cache import biz_cache

    biz_cache.clear()
    yield
    biz_cache.clear()


# ══════════════════════════════════════════════════════════════════════
# ① 并发收敛
# ══════════════════════════════════════════════════════════════════════
def test_screen_runs_concurrently(monkeypatch):
    import src.core.decision_pioneer as dp

    idents: set[int] = set()
    lock = threading.Lock()

    def _f(symbol, market="CN"):
        with lock:
            idents.add(threading.get_ident())
        time.sleep(0.05)
        return _fake_pioneer()(symbol, market)

    monkeypatch.setattr(dp, "fetch_decision_pioneer", _f)
    symbols = [f"{600000 + i:06d}" for i in range(20)]

    t0 = time.monotonic()
    out = sp._screen(symbols)
    elapsed = time.monotonic() - t0

    assert out["total"] == 20 and len(out["rows"]) == 20
    assert len(idents) > 1, "应动用多个线程(并发)"
    # 串行约 20×0.05=1.0s; 并发 10 worker 应显著更快(< 0.6s 留足余量)
    assert elapsed < 0.6, f"并发未生效(耗时 {elapsed:.2f}s)"


def test_screen_sorts_by_resonance(monkeypatch):
    import src.core.decision_pioneer as dp

    def _f(symbol, market="CN"):
        strong = symbol.endswith("1")
        return {
            "gs": {"state": "G区" if strong else "S区", "signal": "G", "pending": False},
            "institution_activity": {"activity": 8.0 if strong else 1.0, "level": "大牛"},
            "l2": {"available": True, "zjl_hb": 1e4 if strong else -1e4},
            "main_intent": None,
        }

    monkeypatch.setattr(dp, "fetch_decision_pioneer", _f)
    out = sp._screen(["600001", "600002", "600003"])
    assert out["rows"][0]["resonance"] == "强"
    assert out["rows"][-1]["resonance"] == "无"


# ══════════════════════════════════════════════════════════════════════
# ② 缓存命中零外呼
# ══════════════════════════════════════════════════════════════════════
def test_screen_endpoint_cache_hit_zero_calls(monkeypatch):
    import src.core.decision_pioneer as dp

    calls = {"n": 0}

    def _f(symbol, market="CN"):
        calls["n"] += 1
        return _fake_pioneer()(symbol, market)

    monkeypatch.setattr(dp, "fetch_decision_pioneer", _f)
    req = sp.StockPoolScreenRequest(symbols=["600519", "000001"])

    r1 = sp.screen_stock_pool(req)
    assert calls["n"] == 2
    # 第二次: 即便取数会抛错也应命中缓存(不再外呼)
    monkeypatch.setattr(dp, "fetch_decision_pioneer",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("should not refetch")))
    r2 = sp.screen_stock_pool(req)
    assert r2 == r1 and calls["n"] == 2


def test_screen_endpoint_invalid_symbols_empty():
    r = sp.screen_stock_pool(sp.StockPoolScreenRequest(symbols=["abc", "12"]))
    assert r == {"rows": [], "total": 0, "truncated": False}


# ══════════════════════════════════════════════════════════════════════
# ③ decision_cache 预落库基底降级(口径不兼容字段如实 None)
# ══════════════════════════════════════════════════════════════════════
def test_row_from_decision_base_maps_compatible_fields(monkeypatch):
    import src.core.decision_cache as dc

    monkeypatch.setattr(dc, "get_cached_decision", lambda s, m="CN": {
        "payload": {"parts": {"trend": "G区间", "activity": 7.5, "fund_net": 123456.0}},
    })
    row = sp._row_from_decision_base("600519")
    assert row is not None
    assert row["gs_state"] == "G区"          # trend 标签 → 已确认区间
    assert row["activity"] == 7.5
    assert row["source"] == "decision_cache"
    # 基底不携带 / 口径不符 → 一律 None(不编造)
    assert row["gs_signal"] is None
    assert row["l2_net"] is None
    assert row["gs_pending"] is False


def test_row_from_decision_base_no_signal_returns_none(monkeypatch):
    import src.core.decision_cache as dc

    monkeypatch.setattr(dc, "get_cached_decision", lambda s, m="CN": {
        "payload": {"parts": {"trend": "无数据", "activity": None}},
    })
    assert sp._row_from_decision_base("600519") is None


def test_row_of_falls_back_to_base_then_none(monkeypatch):
    import src.core.decision_pioneer as dp

    monkeypatch.setattr(dp, "fetch_decision_pioneer", lambda s, m="CN": None)
    monkeypatch.setattr(sp, "_row_from_decision_base", lambda s: {
        "symbol": s, "resonance": "弱", "score": 1, "source": "decision_cache",
        "activity": 7.5, "activity_level": "大牛", "gs_state": "G区",
        "gs_signal": None, "gs_pending": False, "l2_net": None,
    })
    row = sp._row_of("600519")
    assert row["source"] == "decision_cache"

    # 基底也拿不到 → 显式"无"
    monkeypatch.setattr(sp, "_row_from_decision_base", lambda s: None)
    row2 = sp._row_of("600519")
    assert row2["resonance"] == "无" and row2["source"] == "none"
    assert row2["l2_net"] is None and row2["activity"] is None
