# -*- coding: utf-8 -*-
"""thsdk 进程级熔断器 + 并发信号量(2026-09-03 v0.4.73)。

事故根因(v0.4.72): thsdk 内部 3 轮退避 × 30s 超时 = 单次 90s,
多路并发调用时资源被打爆。本模块在 thsdk 调用外层包:
- 熔断器: 连续失败 >= THRESHOLD → 进入冷却 COOLDOWN 秒 → 期间调用直接返回 None
- 半开: 冷却后下一次调用放行(探测), 成功→关闭, 失败→重新开
- 并发信号量: 同时最多 SEMAPHORE 路 thsdk 调用, 超出排队(防无限堆)

调用方用法:
    from src.core.thsdk_breaker import thsdk_call
    result = thsdk_call(lambda: expensive_thsdk_call(), default=None)
    # 内部: 先检查 is_open() → 拿信号量 → 调函数 → 成功 record_success/失败 record_failure
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 失败阈值: 连续 N 次失败后熔断打开
THRESHOLD = 5
# 冷却秒数: 熔断打开后, 多少秒内直接跳过 thsdk
COOLDOWN_S = 60
# 并发上限: 同时最多 N 路 thsdk 调用, 超出排队
SEMAPHORE = 3


class _Breaker:
    """进程级三态熔断器(closed/open/half_open), 线程安全。"""

    def __init__(self, threshold: int = THRESHOLD, cooldown: float = COOLDOWN_S):
        self._lock = threading.Lock()
        self._state = "closed"  # closed | open | half_open
        self._failures = 0
        self._last_fail = 0.0
        self.threshold = threshold
        self.cooldown = cooldown

    def is_open(self) -> bool:
        """是否熔断中(True=跳过 thsdk)。半开探测时返回 False 让一次调用通过。"""
        with self._lock:
            if self._state == "open":
                if time.time() - self._last_fail > self.cooldown:
                    self._state = "half_open"
                    logger.info("thsdk 熔断半开: 放行一次探测调用")
                    return False
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            if self._state != "closed":
                logger.info("thsdk 熔断关闭(恢复)")
            self._state = "closed"
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._last_fail = time.time()
            if self._state == "half_open":
                # 半开探测失败 → 重新打开(不重置计数, 持续恶化信号)
                self._state = "open"
                logger.warning("thsdk 熔断半开探测失败, 重新打开(冷却 %ss)", self.cooldown)
            elif self._failures >= self.threshold:
                self._state = "open"
                logger.warning("thsdk 熔断打开: 连续 %d 次失败, 冷却 %ss",
                               self._failures, self.cooldown)

    def status(self) -> dict:
        """诊断用: 当前状态/失败计数/距上次失败秒数。"""
        with self._lock:
            return {
                "state": self._state,
                "failures": self._failures,
                "seconds_since_last_fail": round(time.time() - self._last_fail, 1) if self._last_fail else None,
                "threshold": self.threshold,
                "cooldown_s": self.cooldown,
            }


# 进程级单例 + 并发信号量
_breaker = _Breaker()
_sem = threading.Semaphore(SEMAPHORE)


@contextmanager
def _slot():
    """并发信号量: 同时最多 SEMAPHORE 路 thsdk 调用。acquire 失败时阻塞排队(非熔断)。"""
    _sem.acquire()
    try:
        yield
    finally:
        _sem.release()


def thsdk_call(fn: Callable[[], T], default: Any = None, swallow_exceptions: tuple = (Exception,)) -> Any:
    """thsdk 调用包装: 熔断检查 → 并发排队 → 执行 → 记结果。

    Args:
        fn: thsdk 调用 lambda/函数
        default: 熔断打开或异常时返回的默认值(默认 None)
        swallow_exceptions: 视为"thsdk 失败"的异常类型元组, 默认全部 Exception
    Returns:
        fn() 结果; 熔断中或异常时返回 default
    """
    if _breaker.is_open():
        logger.debug("thsdk 熔断中, 跳过调用(直接返回 default)")
        return default
    with _slot():
        try:
            result = fn()
        except swallow_exceptions as e:
            _breaker.record_failure()
            logger.warning("thsdk 调用失败(连续 %d): %s: %s", _breaker._failures, type(e).__name__, str(e)[:100])
            return default
    _breaker.record_success()
    return result


def breaker_status() -> dict:
    """诊断: 返回熔断器当前状态(供健康检查/监控用)。"""
    return _breaker.status()
