# -*- coding: utf-8 -*-
"""市场主线接口的**响应时间**钉子（2026-09-19 打磨）。

## 事故
用户报"页面卡"。实测 `/api/market/mainline`：**冷启动 21s / 16.5s**、命中缓存 0.8s。
原实现是**同步冷启动** + 30s TTL → 缓存每半分钟过期一次，过期后的第一个请求被阻塞 16~37s
（拉涨停池聚合），页面一直转圈。

## 修法
`stale-while-revalidate`：过期先返回旧值（毫秒级）+ 后台线程刷新（单飞防惊群）；
TTL 30s→180s；完全无缓存或旧值超过 30 分钟才同步取。另加每 3 分钟的预热 job。

## 这里钉什么
1. **缓存过期时不得阻塞**：过期后立刻返回（<0.2s）且带 `stale=True` + 数据年龄；
2. **不假装数据是新的**：stale 时必须给 `age_s`（诚实口径 —— 旧数据要标出来）；
3. **后台刷新真的会发生**：触发一次过期返回后，缓存最终被更新（年龄归零）；
4. **单飞**：并发 N 个过期请求只回源一次（不能每个请求都去拉 20s 的涨停池）。
"""
from __future__ import annotations

import threading
import time

import pytest

from src.web.api import market_mainline as mm


@pytest.fixture(autouse=True)
def _clean_cache():
    mm.clear_market_mainline_cache()
    yield
    mm.clear_market_mainline_cache()


def _fake_payload(tag: str) -> dict:
    return {"total_groups": 1, "ranked_groups": [], "unranked": [], "note": tag, "cache_ts": time.time()}


def test_fresh_cache_returns_immediately(monkeypatch):
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return _fake_payload(f"v{calls['n']}")

    monkeypatch.setattr(mm, "_fetch_mainline", fake_fetch)
    first = mm.get_market_mainline()
    assert first["note"] == "v1" and calls["n"] == 1
    t0 = time.perf_counter()
    second = mm.get_market_mainline()
    assert (time.perf_counter() - t0) < 0.05, "命中缓存还慢, 说明又回源了"
    assert second["note"] == "v1" and calls["n"] == 1
    # 契约一致性: 新鲜命中也要给 stale/age_s(消费方不用写两套判断)
    assert second["stale"] is False
    assert isinstance(second["age_s"], float) and second["age_s"] >= 0


def test_stale_cache_does_not_block(monkeypatch):
    """**核心判据**：过期后必须立刻返回旧值（毫秒级），不能被 16~37s 的回源卡住。"""
    slow_started = threading.Event()
    release = threading.Event()

    def slow_fetch():
        slow_started.set()
        release.wait(5)  # 模拟"拉涨停池要 20s"
        return _fake_payload("v2")

    monkeypatch.setattr(mm, "_fetch_mainline", lambda: _fake_payload("v1"))
    assert mm.get_market_mainline()["note"] == "v1"

    # 手动把缓存时间拨旧(超过 TTL, 但没到 STALE_MAX)
    with mm._cache_lock:
        mm._cache["mainline:top20"] = (time.monotonic() - (mm._CACHE_TTL_S + 5), _fake_payload("v1"))

    monkeypatch.setattr(mm, "_fetch_mainline", slow_fetch)
    t0 = time.perf_counter()
    out = mm.get_market_mainline()
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.2, f"过期时被阻塞了 {elapsed:.2f}s —— SWR 没生效"
    assert out["note"] == "v1", "过期时应先返回旧值"
    assert out.get("stale") is True, "必须标注 stale, 不能假装是新的"
    assert out.get("age_s", 0) >= mm._CACHE_TTL_S, "必须给出数据年龄"
    release.set()


def test_background_refresh_updates_cache(monkeypatch):
    """过期返回旧值之后，后台刷新最终把缓存换新（年龄归零）。"""
    monkeypatch.setattr(mm, "_fetch_mainline", lambda: _fake_payload("v1"))
    mm.get_market_mainline()
    with mm._cache_lock:
        mm._cache["mainline:top20"] = (time.monotonic() - (mm._CACHE_TTL_S + 5), _fake_payload("v1"))

    monkeypatch.setattr(mm, "_fetch_mainline", lambda: _fake_payload("v2"))
    out = mm.get_market_mainline()
    assert out["note"] == "v1"  # 这一次拿旧值
    for _ in range(50):  # 等后台线程
        time.sleep(0.05)
        with mm._cache_lock:
            hit = mm._cache.get("mainline:top20")
        if hit and hit[1].get("note") == "v2":
            break
    with mm._cache_lock:
        hit = mm._cache.get("mainline:top20")
    assert hit and hit[1]["note"] == "v2", "后台刷新没有更新缓存"


def test_concurrent_stale_requests_single_flight(monkeypatch):
    """并发过期请求只回源一次（单飞），否则 10 个标签页会同时拉 10 次 20s 的涨停池。"""
    calls = {"n": 0}
    gate = threading.Event()

    def slow_fetch():
        calls["n"] += 1
        gate.wait(5)
        return _fake_payload("new")

    monkeypatch.setattr(mm, "_fetch_mainline", lambda: _fake_payload("old"))
    mm.get_market_mainline()
    with mm._cache_lock:
        mm._cache["mainline:top20"] = (time.monotonic() - (mm._CACHE_TTL_S + 5), _fake_payload("old"))
    monkeypatch.setattr(mm, "_fetch_mainline", slow_fetch)

    threads = [threading.Thread(target=mm.get_market_mainline) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    gate.set()
    time.sleep(0.4)
    assert calls["n"] == 1, f"回源了 {calls['n']} 次 —— 单飞失效"


def test_no_cache_at_all_still_works(monkeypatch):
    """进程刚起、完全没缓存时允许同步取一次（这一次用户要等，但只此一次）。"""
    monkeypatch.setattr(mm, "_fetch_mainline", lambda: _fake_payload("cold"))
    out = mm.get_market_mainline()
    assert out["note"] == "cold" and out.get("stale") is False
