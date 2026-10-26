# -*- coding: utf-8 -*-
"""市场主线接口的**响应时间**钉子（2026-09-19 打磨; 2026-10-14 缓存收口改写）。

## 事故
用户报"页面卡"。实测 `/api/market/mainline`：**冷启动 21s / 16.5s**、命中缓存 0.8s。
原实现是**同步冷启动** + 30s TTL → 缓存每半分钟过期一次，过期后的第一个请求被阻塞
16~37s（拉涨停池聚合），页面一直转圈。

## 修法
`stale-while-revalidate`：过期先返回旧值（毫秒级）+ 后台线程刷新（单飞防惊群）；
TTL 30s→180s；只有"完全没数据"才同步取。另加每 3 分钟的预热 job。

## 2026-10-14 缓存收口
承载缓存的容器从**模块内进程 dict**换成 `biz_cache`（L1 内存 + L2 Redis）——
多 worker/多容器共享、重启不丢（读库兜底见 test_market_mainline_snapshot.py）。
**SWR 契约不变**，本文件改用 biz_cache 驱动，继续钉住四件事：
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
from src.web.cache.biz_cache import biz_cache


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    """关 L2(不连真 Redis) + 清 biz_cache L1 / 响应缓存 / 单飞标志 / 库快照分支。

    本文件只钉进程内 SWR 行为;"读库兜底/stale 标注"由 test_market_mainline_snapshot.py
    覆盖, 这里把读库分支关掉, 免得上一用例残留的库行干扰。
    """
    monkeypatch.setattr(biz_cache, "_enabled", False, raising=False)
    monkeypatch.setattr(mm, "_read_daily_snapshot", lambda: None)
    # 本文件只钉 SWR; 后台线程的"落库"换成 no-op, 免得往测试库里写行干扰别的用例。
    monkeypatch.setattr(mm, "_try_write_daily_snapshot_async", lambda p: None)
    biz_cache._l1.clear()
    mm.clear_market_mainline_cache()
    mm._refreshing = False
    yield
    biz_cache._l1.clear()
    mm.clear_market_mainline_cache()
    mm._refreshing = False


def _payload(tag: str, age_s: float = 0.0) -> dict:
    """构造一份载荷: cache_ts 决定它的"数据年龄"(SWR 判断依据)。"""
    return {
        "total_groups": 1,
        "ranked_groups": [],
        "unranked": [],
        "note": tag,
        "cache_ts": time.time() - age_s,
    }


def _seed(tag: str, age_s: float) -> None:
    """把一份(可能过期的)载荷写进 biz_cache, 模拟"缓存里有旧值"。

    TTL 取陈旧上限 —— SWR 要旧值活到能返给用户, 过期与否由载荷年龄判定。
    """
    biz_cache.set_json(mm._CACHE_KEY, _payload(tag, age_s), ttl=int(mm._STALE_MAX_S))


def test_fresh_cache_returns_immediately(monkeypatch):
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        return _payload(f"v{calls['n']}")

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
        return _payload("v2")

    monkeypatch.setattr(mm, "_fetch_mainline", lambda: _payload("v1"))
    assert mm.get_market_mainline()["note"] == "v1"

    # 把缓存里的载荷拨旧(超过 TTL, 但没到 STALE_MAX)
    _seed("v1", mm._CACHE_TTL_S + 5)

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
    monkeypatch.setattr(mm, "_fetch_mainline", lambda: _payload("v1"))
    mm.get_market_mainline()
    _seed("v1", mm._CACHE_TTL_S + 5)

    monkeypatch.setattr(mm, "_fetch_mainline", lambda: _payload("v2"))
    out = mm.get_market_mainline()
    assert out["note"] == "v1"  # 这一次拿旧值
    for _ in range(50):  # 等后台线程
        time.sleep(0.05)
        hit = biz_cache.get_json(mm._CACHE_KEY)
        if isinstance(hit, dict) and hit.get("note") == "v2":
            break
    hit = biz_cache.get_json(mm._CACHE_KEY)
    assert isinstance(hit, dict) and hit["note"] == "v2", "后台刷新没有更新缓存"


def test_concurrent_stale_requests_single_flight(monkeypatch):
    """并发过期请求只回源一次（单飞），否则 10 个标签页会同时拉 10 次 20s 的涨停池。"""
    calls = {"n": 0}
    gate = threading.Event()

    def slow_fetch():
        calls["n"] += 1
        gate.wait(5)
        return _payload("new")

    monkeypatch.setattr(mm, "_fetch_mainline", lambda: _payload("old"))
    mm.get_market_mainline()
    _seed("old", mm._CACHE_TTL_S + 5)
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
    """进程刚起、完全没缓存且库里也没有时允许同步取一次（这一次用户要等，但只此一次）。"""
    monkeypatch.setattr(mm, "_fetch_mainline", lambda: _payload("cold"))
    out = mm.get_market_mainline()
    assert out["note"] == "cold" and out.get("stale") is False


def test_stale_response_shape_keeps_cache_ts_and_age():
    """stale 响应仍带原始 cache_ts(下游可自行算年龄) + stale/age_s 三件套。"""
    _seed("old", mm._CACHE_TTL_S + 30)
    out = mm.get_market_mainline()
    assert out["stale"] is True
    assert out["age_s"] >= mm._CACHE_TTL_S
    assert isinstance(out["cache_ts"], float)
