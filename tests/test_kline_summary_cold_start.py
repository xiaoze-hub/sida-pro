# -*- coding: utf-8 -*-
"""GET /api/klines/{symbol}/summary 冷启动优化(B1 P2 首屏慢)回归测试。

背景: 冷启动原为「主力意图逐笔翻页(硬超时) + 盘口 thsdk(硬超时) + wencai(硬超时)」
**串行相加 ~26s**, 超过前端 fetchAPI 的 20s abort ⇒ /portfolio 自选股 sparkline 长期空列。
修法: ① 响应缓存迁到 biz_cache(L1 内存 + L2 Redis); ② single-flight 防并发雷群;
③ 冷链四个互不依赖的重活并发化。本文件钉住这三点 + 缓存降级/缺失行为不退化。

覆盖:
  ① 冷/热两次调用返回完全一致;
  ② 缓存命中不再重算(mock 计数);
  ③ Redis 不可用 / biz_cache 降级 → 仍正确返回;
  ④ 数据缺失时行为与优化前一致(原样返回降级载荷, 不编造);
  ⑤ 冷链四个重活并发化(冷启动 ≈ 最长一条, 非串行相加);
  ⑥ single-flight: 并发相同 symbol 只重算一次;
  ⑦ summary_cache 超限载荷不再被静默丢弃 / 也不再抛异常;
  ⑧ refresh=1 仍能跳过全部缓存强制重算。
"""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.models.market import MarketCode  # noqa: E402
from src.web.api import klines as kapi  # noqa: E402
from src.web.cache.biz_cache import biz_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_summary_caches(monkeypatch):
    """隔离: 每次清 biz_cache L1、single-flight 锁、PG 缓存。测试内不连真 Redis。

    (skill 提醒: 给缓存加层会悄悄破坏测试隔离 —— 同一端点同参数被多个用例打,
     后面的用例会读到前面回填的载荷, 断言无声翻转。故 autouse 清干净。)
    """
    monkeypatch.setattr(biz_cache, "_enabled", False, raising=False)
    biz_cache._l1.clear()
    kapi._summary_locks.clear()
    kapi._clear_summary_caches()
    yield
    biz_cache._l1.clear()
    kapi._summary_locks.clear()
    kapi._clear_summary_caches()


def _full_payload(symbol: str, close=12.3) -> dict:
    return {
        "symbol": symbol,
        "market": "CN",
        "summary": {"close": close, "trend": "多头排列"},
        "main_intent": "主力净流入",
        "main_intent_structured": {"direction": "buy"},
        "mainflow_tri": {"agree": True, "consensus_wan": 1.0, "spread_pct": 0.1, "n_ok": 3, "sources": {}},
        "gs_signals": [{"date": "2026-09-01", "side": "G"}],
        "fund_flow": [{"date": "2026-09-01", "ming_net": None, "dark_net": 1.0}],
        "events": [],
        "orderbook": None,
        "unlock_levels": None,
        "chips": None,
        "activity_series": None,
        "resonance": {"available": False},
        "dark_clusters": {"available": False, "note": "未接入"},
    }


def test_cold_and_hot_identical_and_computed_once(monkeypatch):
    """①+② 冷/热返回完全一致, 且第二次命中缓存不重算。"""
    calls = {"n": 0}

    def fake_compute(symbol, market_code):
        calls["n"] += 1
        return _full_payload(symbol)

    monkeypatch.setattr(kapi, "_compute_kline_summary", fake_compute)

    cold = kapi.get_kline_summary("605580", market="CN")
    hot = kapi.get_kline_summary("605580", market="CN")

    assert cold == hot, "冷/热两次调用必须逐字段一致"
    assert cold == _full_payload("605580")
    assert calls["n"] == 1, f"第二次应命中缓存, 实际重算 {calls['n']} 次"


def test_redis_unavailable_still_returns_correctly(monkeypatch):
    """③ biz_cache 的 L2 Redis 不可达 → 退回纯 L1, 行为不变(不抛、正确返回)。"""
    monkeypatch.setattr(biz_cache, "_enabled", True, raising=False)
    monkeypatch.setattr(biz_cache, "_ensure_redis", lambda: None)  # Redis 探活恒失败

    calls = {"n": 0}

    def fake_compute(symbol, market_code):
        calls["n"] += 1
        return _full_payload(symbol, close=9.9)

    monkeypatch.setattr(kapi, "_compute_kline_summary", fake_compute)

    r1 = kapi.get_kline_summary("000001", market="CN")
    r2 = kapi.get_kline_summary("000001", market="CN")

    assert r1 == r2 == _full_payload("000001", close=9.9)
    assert calls["n"] == 1, "Redis 挂了 L1 仍应命中, 不得因 Redis 失败而重算"


def test_missing_data_returned_verbatim_no_fabrication(monkeypatch):
    """④ 数据缺失时原样返回降级载荷, 绝不编造(与优化前一致)。"""
    degraded = {
        "symbol": "300285",
        "market": "CN",
        "summary": {"error": "无K线数据"},
        "main_intent": None,
        "main_intent_structured": None,
        "mainflow_tri": {"agree": None, "consensus_wan": None, "spread_pct": None, "n_ok": 0, "sources": {}},
        "gs_signals": None,
        "fund_flow": None,
        "events": None,
        "orderbook": None,
        "unlock_levels": None,
        "chips": None,
        "activity_series": None,
        "resonance": {"available": False},
        "dark_clusters": {"available": False, "note": "未接入"},
    }
    monkeypatch.setattr(kapi, "_compute_kline_summary", lambda s, m: dict(degraded))

    out = kapi.get_kline_summary("300285", market="CN")

    assert out == degraded
    assert out["main_intent"] is None
    assert out["gs_signals"] is None
    assert out["summary"] == {"error": "无K线数据"}


def test_cold_path_runs_independent_work_concurrently(monkeypatch):
    """⑤ 冷链四个重活并发 → 冷启动 ≈ 最长一条, 而非串行相加。"""
    d = 0.3

    def sleep_intent(symbol, market_code):
        time.sleep(d)
        return None, None

    monkeypatch.setattr(kapi, "_enrich_main_intent", sleep_intent)
    monkeypatch.setattr(kapi, "_enrich_dark_clusters", lambda s: (time.sleep(d), {"available": False})[1])
    monkeypatch.setattr(kapi, "_enrich_mainflow_tri", lambda s, m: (time.sleep(d), {"agree": None})[1])
    monkeypatch.setattr(kapi, "_build_layer_data", lambda s, m: (time.sleep(d), {"gs_signals": None})[1])

    class _FakeCollector:
        def __init__(self, market_code):
            pass

        def get_kline_summary(self, symbol):
            return {"close": 1.0}

    monkeypatch.setattr(kapi, "KlineCollector", _FakeCollector)

    t0 = time.perf_counter()
    out = kapi.get_kline_summary("600519", market="CN")
    dt = time.perf_counter() - t0

    # 串行 = 4×0.3 = 1.2s; 并发 ≈ 0.3s。留足 CI 余量, 断言 < 0.9s。
    assert dt < 0.9, f"冷启动应并发执行, 实测 {dt:.2f}s(串行约 1.2s)"
    assert dt >= d * 0.9
    assert out["symbol"] == "600519"
    assert out["gs_signals"] is None


def test_single_flight_dedupes_concurrent_cold(monkeypatch):
    """⑥ 并发相同 symbol 的冷启动只真正重算一次(防雷群打满线程池)。"""
    calls = {"n": 0}

    def fake_compute(symbol, market_code):
        calls["n"] += 1
        time.sleep(0.3)  # 拉长窗口, 让 5 个线程都落在 miss 上
        return {"symbol": symbol, "market": market_code.value, "summary": {}}

    monkeypatch.setattr(kapi, "_compute_kline_summary", fake_compute)

    results = []

    def worker():
        results.append(kapi.get_kline_summary("601318", market="CN"))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1, f"并发应单飞只算一次, 实际 {calls['n']} 次"
    assert len(results) == 5
    assert all(r == results[0] for r in results)


def test_refresh_bypasses_all_cache_layers(monkeypatch):
    """⑧ refresh=1 仍跳过所有缓存强制重算并回填。"""
    calls = {"n": 0}

    def fake_compute(symbol, market_code):
        calls["n"] += 1
        return {"symbol": symbol, "market": market_code.value, "n": calls["n"]}

    monkeypatch.setattr(kapi, "_compute_kline_summary", fake_compute)

    a = kapi.get_kline_summary("000002", market="CN")
    b = kapi.get_kline_summary("000002", market="CN")
    c = kapi.get_kline_summary("000002", market="CN", refresh=True)

    assert a == b
    assert calls["n"] == 2, "refresh 应强制重算"
    assert c["n"] == 2
    # refresh 后回填: 再普通调用应命中 c
    assert kapi.get_kline_summary("000002", market="CN") == c


def test_summary_cache_large_payload_persists_intact():
    """⑦ 大载荷(超旧 50KB 上限)应完整落库 —— 旧实现在此静默丢弃(缓存永不命中)。"""
    from src.core.summary_cache import (
        SUMMARY_PAYLOAD_MAX,
        get_cached_summary,
        put_cached_summary,
    )

    big = {"symbol": "BIG", "blob": "x" * 70_000}  # > 50KB, < 新 1MB 上限
    assert len(json.dumps(big)) > 50_000
    assert SUMMARY_PAYLOAD_MAX >= 1_000_000

    put_cached_summary("BIG", "CN", big, ttl_s=300)
    back = get_cached_summary("BIG", "CN", ttl_s=300)

    assert back == big, "大载荷应完整落库(旧实现 json.loads 截断串 → 抛 → 整条丢弃)"


def test_summary_cache_overflow_skips_without_raising(monkeypatch):
    """⑦ 极端超限: 跳过落库(不写残缺数据), 且绝不抛异常。"""
    from src.core import summary_cache as sc

    monkeypatch.setattr(sc, "SUMMARY_PAYLOAD_MAX", 10)
    payload = {"a": 1, "b": "y" * 100}

    sc.put_cached_summary("OVF", "CN", payload, ttl_s=300)  # 不得抛

    # 既不落残缺数据, 也不返回任何东西(下次走重算)。
    assert sc.get_cached_summary("OVF", "CN", ttl_s=300) is None
