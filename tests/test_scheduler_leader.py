"""Q1 调度器选主测试(2026-08-23)。"""
from __future__ import annotations

from types import SimpleNamespace

from src.core import scheduler_leader as sl


class FakeRedis:
    def __init__(self, holder: dict):
        self._h = holder

    def set(self, key, value, nx=False, ex=None):
        if nx and self._h.get("val") not in (None, value):
            return None
        if nx and self._h.get("val") == value:
            return None  # NX: 已存在(即使是自己)不覆盖
        self._h["val"] = value
        return True

    def get(self, key):
        return self._h["val"].encode() if self._h.get("val") else None

    def expire(self, key, ttl):
        self._h["ttl_at"] = ttl
        return True


def test_force_env(monkeypatch):
    monkeypatch.setenv("SIDA_ENABLE_SCHEDULERS", "1")
    ok, why = sl.try_acquire()
    assert ok and "强制" in why
    monkeypatch.setenv("SIDA_ENABLE_SCHEDULERS", "0")
    ok, why = sl.try_acquire()
    assert not ok


def test_acquire_then_second_worker_yields(monkeypatch):
    holder = {}
    monkeypatch.delenv("SIDA_ENABLE_SCHEDULERS", raising=False)
    monkeypatch.setattr(sl, "_client", lambda: FakeRedis(holder))
    monkeypatch.setattr(sl, "ACQUIRE_RETRY_SECONDS", 0)  # 不重试, 立刻让位
    ok1, why1 = sl.try_acquire()
    assert ok1
    wid2 = "other-host:999"
    monkeypatch.setattr(sl, "worker_id", lambda: wid2)
    ok2, why2 = sl.try_acquire()
    assert not ok2 and "让位" in why2


def test_reentry_after_reload(monkeypatch):
    holder = {}
    monkeypatch.delenv("SIDA_ENABLE_SCHEDULERS", raising=False)
    monkeypatch.setattr(sl, "_client", lambda: FakeRedis(holder))
    ok1, _ = sl.try_acquire()
    assert ok1
    wid = sl.worker_id()
    # FakeRedis 的 NX 语义: 自己持有也返回 None → 走 get 比对续期分支
    ok2, why2 = sl.try_acquire()
    assert ok2 and "续期" in why2


def test_redis_unavailable_falls_back(monkeypatch):
    def boom():
        raise ConnectionError("down")

    monkeypatch.delenv("SIDA_ENABLE_SCHEDULERS", raising=False)
    monkeypatch.setattr(sl, "_client", boom)
    ok, why = sl.try_acquire()
    assert ok and "回退" in why
