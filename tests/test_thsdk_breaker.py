# -*- coding: utf-8 -*-
"""thsdk_breaker 单测(2026-09-03 v0.4.73)。"""
import time
from src.core.thsdk_breaker import _Breaker, thsdk_call, breaker_status


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
    try:
        mod_breaker._state = "open"
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
