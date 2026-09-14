# -*- coding: utf-8 -*-
"""thsdk_breaker 单测(2026-09-03 v0.4.73 / 2026-09-18 并发治理)。"""
import threading
import time

import pytest

from src.core.thsdk_breaker import (
    SEMAPHORE,
    _Breaker,
    breaker_status,
    call_with_hard_timeout,
    reset_for_tests,
    thsdk_call,
)


@pytest.fixture(autouse=True)
def _clean_breaker():
    """每个用例前后复位熔断器/信号量/计数, 防状态串染。"""
    reset_for_tests()
    yield
    reset_for_tests()


def test_closed_starts():
    b = _Breaker()
    assert b.is_open() is False
    assert b.status()["state"] == "closed"


def test_opens_after_threshold_failures():
    b = _Breaker(threshold=3, cooldown=60)
    assert b.is_open() is False
    b.record_failure(); b.record_failure()
    assert b.is_open() is False
    b.record_failure()  # 第 3 次
    assert b.is_open() is True
    assert b.status()["state"] == "open"


def test_success_resets_failures():
    b = _Breaker(threshold=3, cooldown=60)
    b.record_failure(); b.record_failure()
    b.record_success()  # 重置
    assert b.is_open() is False
    assert b.status()["failures"] == 0


def test_half_open_after_cooldown():
    b = _Breaker(threshold=1, cooldown=0.1)
    b.record_failure()
    assert b.is_open() is True
    time.sleep(0.15)
    # 冷却后下次 is_open() 进入 half_open 并放行
    assert b.is_open() is False
    assert b.status()["state"] == "half_open"


def test_half_open_failure_reopens():
    b = _Breaker(threshold=1, cooldown=0.05)
    b.record_failure()
    time.sleep(0.1)
    b.is_open()  # 进入 half_open
    b.record_failure()  # 半开探测失败
    assert b.status()["state"] == "open"


def test_thsdk_call_success():
    result = thsdk_call(lambda: 42, default=0)
    assert result == 42
    assert breaker_status()["state"] == "closed"


def test_thsdk_call_exception_returns_default():
    def boom():
        raise ConnectionError("network down")
    result = thsdk_call(boom, default="fallback")
    assert result == "fallback"
    # 失败应记入计数
    assert breaker_status()["failures"] >= 1


def test_thsdk_call_skips_when_open():
    # 直接把模块级 _breaker 设为 open 状态(测试完还原)
    from src.core.thsdk_breaker import _breaker as mod_breaker
    saved_state = mod_breaker._state
    saved_failures = mod_breaker._failures
    saved_last = mod_breaker._last_fail
    try:
        mod_breaker._state = "open"
        # is_open() 在冷却结束后会转 half_open; 必须把 _last_fail 钉在"现在",
        # 否则 time.time()-0 > cooldown 立刻半开, 用例测不到"跳过"
        mod_breaker._last_fail = time.time()
        called = []
        def fn():
            called.append(1)
            return "should_not_run"
        result = thsdk_call(fn, default="skipped")
        assert result == "skipped"
        assert called == []  # 根本没调用 fn(熔断跳过)
    finally:
        mod_breaker._state = saved_state
        mod_breaker._failures = saved_failures
        mod_breaker._last_fail = saved_last


# ── 2026-09-18 并发/硬超时治理 ──────────────────────────────────────────


def test_hard_timeout_returns_default_without_waiting_hung_call():
    """硬超时: 调用挂住 timeout_s 后立刻返回 default, 不等线程结束。"""
    started = time.monotonic()

    def hang():
        time.sleep(3.0)
        return "should_not_arrive"

    result = call_with_hard_timeout(hang, default="timed_out", timeout_s=0.2)
    elapsed = time.monotonic() - started
    assert result == "timed_out"
    # 必须远小于挂住的 3s —— 证明没在 shutdown(wait=True) 上等
    assert elapsed < 1.0, f"硬超时后仍阻塞 {elapsed:.2f}s(疑似 wait=True)"
    assert breaker_status()["call_timeout"] >= 1


def test_hard_timeout_hung_call_still_holds_semaphore_slot():
    """挂死线程仍占并发槽: 超时后槽不立刻还, 防挂死线程堆出 SEMAPHORE 上限。"""
    release_hang = threading.Event()

    def hang():
        release_hang.wait(timeout=10)
        return "late"

    # 放 SEMAPHORE 路挂死调用
    for _ in range(SEMAPHORE):
        call_with_hard_timeout(hang, default=None, timeout_s=0.1)
    # 此时 3 路都已超时返回, 但线程仍挂住、槽未还
    st = breaker_status()
    assert st["inflight"] >= SEMAPHORE - 1

    # 再来一路: acquire 应超时放弃(不新起线程)
    t0 = time.monotonic()
    result = call_with_hard_timeout(lambda: 1, default="no_slot", timeout_s=1.0,
                                    acquire_timeout_s=0.2)
    assert result == "no_slot"
    assert time.monotonic() - t0 < 1.5
    # 放行挂死线程, 让它们结束并还槽
    release_hang.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and breaker_status()["inflight"] > 0:
        time.sleep(0.05)
    # 结束后槽应还回(计数归零或接近)
    assert breaker_status()["inflight"] < SEMAPHORE


def test_acquire_timeout_returns_default_fast():
    """并发槽满: 等待 acquire_timeout_s 后快速放弃, 不无限排队。"""
    release = threading.Event()

    def block():
        release.wait(timeout=10)
        return 1

    # 占满并发槽(不超时, 让它们一直拿着槽)
    holders = []
    for _ in range(SEMAPHORE):
        t = threading.Thread(
            target=lambda: thsdk_call(block, default=None),
            daemon=True,
        )
        t.start()
        holders.append(t)
    # 等槽占满
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and breaker_status()["inflight"] < SEMAPHORE:
        time.sleep(0.02)
    assert breaker_status()["inflight"] >= SEMAPHORE, "持有方未能占满并发槽"

    t0 = time.monotonic()
    # 不带硬超时的路径也应 acquire 超时快速返回
    result = thsdk_call(lambda: "ok", default="queued_out", acquire_timeout_s=0.2)
    elapsed = time.monotonic() - t0
    assert result == "queued_out"
    assert elapsed < 1.0, f"acquire 未按时放弃, 阻塞 {elapsed:.2f}s"
    assert breaker_status()["acquire_timeout"] >= 1

    release.set()
    for t in holders:
        t.join(timeout=2)


def test_hard_timeout_success_releases_slot_immediately():
    """成功路径立刻还槽(不是等 done 回调)。"""
    result = call_with_hard_timeout(lambda: "ok", default="bad", timeout_s=2.0)
    assert result == "ok"
    assert breaker_status()["inflight"] == 0
    # 还能立刻再拿一槽
    assert call_with_hard_timeout(lambda: "again", default=None, timeout_s=2.0) == "again"


def test_hard_timeout_exception_returns_default_and_records_failure():
    def boom():
        raise RuntimeError("vendor down")

    result = call_with_hard_timeout(boom, default="fallback", timeout_s=2.0)
    assert result == "fallback"
    assert breaker_status()["failures"] >= 1
    assert breaker_status()["inflight"] == 0


def test_thsdk_call_with_timeout_s_uses_hard_timeout_path():
    """thsdk_call(timeout_s=...) 走硬超时: 挂住时按 default 返回。"""
    def hang():
        time.sleep(2.0)
        return "late"

    t0 = time.monotonic()
    result = thsdk_call(hang, default="timed_out", timeout_s=0.15)
    assert result == "timed_out"
    assert time.monotonic() - t0 < 1.0
