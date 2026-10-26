# -*- coding: utf-8 -*-
"""biz_cache 跨实例(跨 worker / 重启)共享语义钉子(2026-10-14 缓存收口)。

背景: 三处回归点(K线 summary/batch、决策先锋辅助指标、市场主线)把结果缓存在
**模块内进程 dict** 里 —— 多 worker 各存各的、重启即丢, 且违反 AGENTS 硬约束
『缓存一律走 `src/web/cache/biz_cache.py`, 禁止业务代码自建』。收口的前提是
"L2 真的共享", 本文件用内存假 Redis 钉死这一点(禁真网络):
  ① 实例 A 写 → 全新实例 B(空 L1)能从 L2 读到 = 跨 worker / 重启不丢;
  ② 落 Redis 的键带 `biz:` 前缀(与限流 u:/i: 与 stream: 天然隔离);
  ③ L2 不可达 → 退回纯 L1(不抛、不阻塞), 且**不假装**跨实例共享(诚实降级)。
"""
from __future__ import annotations

import pytest

from src.web.cache import biz_cache as biz_cache_mod
from src.web.cache.biz_cache import BizCache


class _FakeRedis:
    """内存假 Redis: 只实现 biz_cache 用到的最小面。"""

    def __init__(self, store: dict) -> None:
        self.store = store

    def ping(self) -> bool:
        return True

    def get(self, k: str):
        return self.store.get(k)

    def set(self, k: str, v, ex=None) -> bool:
        self.store[k] = v
        return True

    def ttl(self, k: str) -> int:
        return 30

    def delete(self, *ks: str) -> int:
        return sum(1 for k in ks if self.store.pop(k, None) is not None)

    def scan(self, cursor=0, match=None, count=500):
        return 0, []


@pytest.fixture()
def fake_l2(monkeypatch) -> dict:
    """让任意 BizCache 实例的 L2 指向同一份内存假 Redis。"""
    store: dict = {}
    monkeypatch.setattr(
        BizCache, "_ensure_redis", lambda self: _FakeRedis(store)
    )
    return store


def test_cross_instance_shares_via_l2(fake_l2):
    """① 实例 A 写 → 全新实例 B(空 L1)从 L2 读到 = 跨 worker / 重启不丢。"""
    a = BizCache()
    assert a.set_json("pioneer_indicators:trend:CN:600519", {"v": 1}, ttl=30) is True

    b = BizCache()  # 全新实例: L1 空, 等价于另一个 worker / 重启后的进程
    assert b._l1 == {}, "前提: 新实例 L1 必须为空, 否则测的不是 L2 共享"
    assert b.get_json("pioneer_indicators:trend:CN:600519") == {"v": 1}


def test_l2_key_carries_biz_prefix(fake_l2):
    """② 落 L2 的键必须带 `biz:` 前缀(不误伤限流/stream 的键空间)。"""
    BizCache().set_json("mainline:top20", {"note": "x"}, ttl=30)
    assert "biz:mainline:top20" in fake_l2
    assert "mainline:top20" not in fake_l2


def test_l2_hit_backfills_l1(fake_l2):
    """L2 命中回填 L1: 第二次读不再走 L2(省一次网络)。"""
    a = BizCache()
    a.set_json("k_backfill", {"v": 2}, ttl=30)

    b = BizCache()
    assert b.get_json("k_backfill") == {"v": 2}   # 从 L2 读
    with b._lock:
        assert "k_backfill" in b._l1, "L2 命中应回填 L1"


def test_l2_unavailable_degrades_to_l1_only(monkeypatch):
    """③ L2 不可达 → 退回纯 L1(行为等价于原进程内 dict); 不抛、不阻塞。"""
    monkeypatch.setattr(BizCache, "_ensure_redis", lambda self: None)

    a = BizCache()
    assert a.set_json("k_l1", {"v": 3}, ttl=30) is False  # 尽力写 L2 失败, 但不抛
    assert a.get_json("k_l1") == {"v": 3}                 # L1 仍命中

    b = BizCache()  # 另一实例: L2 挂了就真的共享不了 —— 诚实返回 miss
    assert b.get_json("k_l1") is None


def test_ttl_expiry_drops_l1_entry(monkeypatch):
    """L1 条目过期后必须 miss(不能无限返回旧值), 并向前 L2 询问。"""
    monkeypatch.setattr(BizCache, "_ensure_redis", lambda self: None)
    c = BizCache()
    c.set_json("k_ttl", {"v": 4}, ttl=30)
    assert c.get_json("k_ttl") == {"v": 4}

    # 把 L1 的过期时刻拨到过去(模拟 30s 到了)
    with c._lock:
        exp, val = c._l1["k_ttl"]
        c._l1["k_ttl"] = (exp - 10_000, val)
    assert c.get_json("k_ttl") is None
    with c._lock:
        assert "k_ttl" not in c._l1, "过期条目应从 L1 删除"


def test_global_singleton_is_a_bizcache():
    """业务代码 import 的 `biz_cache` 就是 BizCache 单例(收口口径的唯一入口)。"""
    assert isinstance(biz_cache_mod.biz_cache, BizCache)
    assert biz_cache_mod.biz_cache is BizCache.instance()
