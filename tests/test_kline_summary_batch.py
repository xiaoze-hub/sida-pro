# -*- coding: utf-8 -*-
"""POST /api/klines/summary/batch 缓存收口回归(2026-10-14)。

背景(落库缺口审计): `/klines/summary/batch` 原实现是**逐只裸调
`collector.get_kline_summary()` 现算** —— 绕开 biz_cache/PG 两层缓存(同一只股票从单只
端点刚算过, 批量端点仍从头再算一遍), 且纯串行(N 只 = N 次现算串联)。修法: 单只端点的
三层取数抽成 `_get_or_compute_summary()`, 批量端点**逐只走同一入口** + 线程池并发上限。

本文件钉(禁真网络, mock 掉重算函数与数据库):
  ① 首批算过之后, 第二批**零重算**(命中 biz_cache);
  ② 与单只端点**共享同一份缓存**(单只先算 → 批量不再算);
  ③ 并发上限 `_SUMMARY_BATCH_MAX_WORKERS`(真并发, 但不超上限);
  ④ 同 symbol 重复入参**单飞**(只算一次);
  ⑤ 空列表 / 非法市场语义不变(空数组 / 400)。
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi import HTTPException

from src.web.api import klines as kapi
from src.web.cache.biz_cache import biz_cache


@pytest.fixture(autouse=True)
def _clean_summary_caches(monkeypatch):
    """隔离: 关 L2(不连真 Redis), 清 biz_cache L1 / single-flight 锁 / PG 缓存。"""
    monkeypatch.setattr(biz_cache, "_enabled", False, raising=False)
    biz_cache._l1.clear()
    kapi._summary_locks.clear()
    kapi._clear_summary_caches()
    yield
    biz_cache._l1.clear()
    kapi._summary_locks.clear()
    kapi._clear_summary_caches()


def _payload(symbol: str, market: str = "CN", n: int = 0) -> dict:
    return {
        "symbol": symbol,
        "market": market,
        "summary": {"close": 10.0 + n},
        "main_intent": None,
        "marker": n,
    }


def _req(*pairs) -> kapi.KlineSummaryBatchRequest:
    return kapi.KlineSummaryBatchRequest(
        items=[kapi.KlineSummaryItem(symbol=s, market=m) for s, m in pairs]
    )


def test_batch_second_call_zero_recompute(monkeypatch):
    """① 第二批命中缓存 → 零重算, 且两批返回逐字段一致。"""
    calls = {"n": 0}

    def fake_compute(symbol, market_code):
        calls["n"] += 1
        return _payload(symbol, market_code.value, calls["n"])

    monkeypatch.setattr(kapi, "_compute_kline_summary", fake_compute)

    req = _req(("600519", "CN"), ("000001", "CN"))
    first = kapi.get_kline_summary_batch(req)
    second = kapi.get_kline_summary_batch(req)

    assert first == second
    assert calls["n"] == 2, f"每只应只算一次, 实际重算 {calls['n']} 次"
    assert [r["symbol"] for r in first] == ["600519", "000001"], "响应必须与 items 同序"


def test_batch_shares_cache_with_single_endpoint(monkeypatch):
    """② 单只端点先算过 → 批量端点命中同一份 `summary:{market}:{symbol}` 缓存。"""
    calls = {"n": 0}

    def fake_compute(symbol, market_code):
        calls["n"] += 1
        return _payload(symbol, market_code.value, calls["n"])

    monkeypatch.setattr(kapi, "_compute_kline_summary", fake_compute)

    single = kapi.get_kline_summary("600519", market="CN")
    assert calls["n"] == 1

    batch = kapi.get_kline_summary_batch(_req(("600519", "CN"), ("601318", "CN")))

    assert calls["n"] == 2, "只有没算过的 601318 该重算"
    assert batch[0] == single, "批量端点的 600519 必须就是单只端点那份缓存"


def test_batch_returns_single_endpoint_payload_shape(monkeypatch):
    """返回结构与单只端点一致(不再只有 `summary` 一个子集)。"""
    monkeypatch.setattr(
        kapi, "_compute_kline_summary", lambda s, m: _payload(s, m.value, 7)
    )
    out = kapi.get_kline_summary_batch(_req(("600519", "CN")))
    assert out[0]["symbol"] == "600519"
    assert out[0]["market"] == "CN"
    assert out[0]["summary"] == {"close": 17.0}
    assert out[0]["marker"] == 7


def test_batch_concurrency_bounded_and_real(monkeypatch):
    """③ 真并发(峰值 ≥2), 但不超 `_SUMMARY_BATCH_MAX_WORKERS`。"""
    cap = kapi._SUMMARY_BATCH_MAX_WORKERS
    lock = threading.Lock()
    state = {"cur": 0, "peak": 0, "calls": 0}

    def fake_compute(symbol, market_code):
        with lock:
            state["cur"] += 1
            state["calls"] += 1
            state["peak"] = max(state["peak"], state["cur"])
        time.sleep(0.15)
        with lock:
            state["cur"] -= 1
        return _payload(symbol, market_code.value)

    monkeypatch.setattr(kapi, "_compute_kline_summary", fake_compute)

    pairs = [(f"{600000 + i}", "CN") for i in range(12)]
    out = kapi.get_kline_summary_batch(_req(*pairs))

    assert len(out) == 12
    assert state["calls"] == 12
    assert state["peak"] <= cap, f"峰值并发 {state['peak']} 超过上限 {cap}"
    assert state["peak"] >= 2, "没有并发(退化成串行), 冷启动仍会串行相加"


def test_batch_same_symbol_single_flight(monkeypatch):
    """④ 同一只票重复入参 → single-flight 只算一次(其余等锁后读缓存)。"""
    calls = {"n": 0}

    def fake_compute(symbol, market_code):
        calls["n"] += 1
        time.sleep(0.2)  # 拉长窗口, 让并发线程都落在 miss 上
        return _payload(symbol, market_code.value, calls["n"])

    monkeypatch.setattr(kapi, "_compute_kline_summary", fake_compute)

    out = kapi.get_kline_summary_batch(_req(*[("600519", "CN")] * 8))

    assert calls["n"] == 1, f"同键并发应单飞, 实际重算 {calls['n']} 次"
    assert len(out) == 8 and all(r == out[0] for r in out)


def test_batch_empty_items_returns_empty_list():
    """⑤ 空列表 → 空数组(不触任何取数)。"""
    assert kapi.get_kline_summary_batch(kapi.KlineSummaryBatchRequest(items=[])) == []


def test_batch_bad_market_raises_400(monkeypatch):
    """⑤ 非法市场 → 400(与单只端点同一错误语义)。"""
    monkeypatch.setattr(
        kapi, "_compute_kline_summary", lambda s, m: _payload(s, m.value)
    )
    with pytest.raises(HTTPException) as ei:
        kapi.get_kline_summary_batch(_req(("600519", "XX")))
    assert ei.value.status_code == 400


def test_batch_hk_market_key_isolated_from_cn(monkeypatch):
    """同一代码不同市场 → 不同缓存键(A/H 不能串)。"""
    calls = {"n": 0}

    def fake_compute(symbol, market_code):
        calls["n"] += 1
        return _payload(symbol, market_code.value, calls["n"])

    monkeypatch.setattr(kapi, "_compute_kline_summary", fake_compute)
    out = kapi.get_kline_summary_batch(_req(("600519", "CN"), ("600519", "HK")))
    assert calls["n"] == 2
    assert {r["market"] for r in out} == {"CN", "HK"}
