# -*- coding: utf-8 -*-
"""thsdk 进程级熔断器 + 并发信号量 + 硬超时(2026-09-03 v0.4.73 / 2026-09-18 并发治理)。

事故根因(v0.4.72): thsdk 内部 3 轮退避 × 30s 超时 = 单次 90s,
多路并发调用时资源被打爆。本模块在 thsdk 调用外层包:
- 熔断器: 连续失败 >= THRESHOLD → 进入冷却 COOLDOWN 秒 → 期间调用直接返回 None
- 半开: 冷却后下一次调用放行(探测), 成功→关闭, 失败→重新开
- 并发信号量: 同时最多 SEMAPHORE 路 thsdk 调用; **acquire 带超时**, 满了直接返回
  default(排队线程不再无限堆积 —— v0.6.3 单 worker 116 线程的主因之一)
- 硬超时: 调用挂住时按 CALL_TIMEOUT_S 弃用并返回 default; **挂死线程仍占并发槽**
  直到真正结束(防"超时后又放行新调用"把挂死线程堆到 SEMAPHORE 之上)

调用方用法:
    from src.core.thsdk_breaker import thsdk_call, call_with_hard_timeout
    result = thsdk_call(lambda: expensive_thsdk_call(), default=None)
    result = call_with_hard_timeout(fn, default=None, timeout_s=8.0)
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from contextlib import contextmanager
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 失败阈值: 连续 N 次失败后熔断打开
THRESHOLD = 5
# 冷却秒数: 熔断打开后, 多少秒内直接跳过 thsdk
COOLDOWN_S = 60
# 并发上限: 同时最多 N 路 thsdk 调用(含已超时但线程仍在跑的 abandoned 调用)
SEMAPHORE = 3
# 并发槽 acquire 超时: 排队等不到槽就放弃, 不让等待线程无限堆积
ACQUIRE_TIMEOUT_S = 5.0
# 单次调用硬超时: 行情服务不通时单次可卡 30s, 三轮退避 90s
CALL_TIMEOUT_S = 8.0


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
# BoundedSemaphore: release 超过初始值会抛, 防 double-release
_sem = threading.BoundedSemaphore(SEMAPHORE)
# 诊断计数
_stats_lock = threading.Lock()
_stats = {"acquire_timeout": 0, "call_timeout": 0, "inflight": 0}


def _incr(key: str) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + 1


def _decr_inflight() -> None:
    with _stats_lock:
        _stats["inflight"] = max(0, _stats.get("inflight", 0) - 1)


@contextmanager
def _slot(acquire_timeout_s: float | None = None):
    """并发信号量: 同时最多 SEMAPHORE 路 thsdk 调用。

    acquire 带超时 —— 拿不到槽直接抛 TimeoutError, 让调用方快速失败,
    绝不让等待线程在锁上无限排队(2026-09-18 线程膨胀治理)。
    """
    timeout = ACQUIRE_TIMEOUT_S if acquire_timeout_s is None else acquire_timeout_s
    sem = _sem  # 捕获当前实例
    if not sem.acquire(timeout=timeout):
        _incr("acquire_timeout")
        raise TimeoutError(
            f"thsdk 并发槽已满(上限 {SEMAPHORE}), 等待 {timeout:.1f}s 未获得, 放弃本次调用")
    _incr("inflight")
    try:
        yield
    finally:
        _decr_inflight()
        sem.release()


def call_with_hard_timeout(
    fn: Callable[[], T],
    default: Any = None,
    *,
    timeout_s: float = CALL_TIMEOUT_S,
    acquire_timeout_s: float | None = None,
    swallow_exceptions: tuple = (Exception,),
) -> Any:
    """在专用线程里跑 fn, 超时立即返回 default(**不**等挂死线程结束)。

    并发槽语义(关键):
      - 成功/普通异常: 立刻释放槽
      - 超时: **槽不立刻释放**, 挂到 future done 回调上 —— 挂死线程仍占一个并发位,
        防止"超时后又放行新调用"把挂死线程堆到 SEMAPHORE 之上
        (Python 无法杀线程, 只能等它自己结束再回收槽)
    """
    if not _sem.acquire(timeout=ACQUIRE_TIMEOUT_S if acquire_timeout_s is None else acquire_timeout_s):
        _incr("acquire_timeout")
        logger.warning("thsdk 并发槽已满, 放弃本次调用(硬超时路径)")
        return default
    # 捕获当前实例: reset_for_tests 换掉模块级 _sem/_breaker 后,
    # 迟到的 done 回调必须还到**当初占用的那把**信号量上
    sem = _sem
    breaker = _breaker
    _incr("inflight")
    released = threading.Event()
    release_lock = threading.Lock()

    def _release_once() -> None:
        with release_lock:
            if released.is_set():
                return
            released.set()
        _decr_inflight()
        sem.release()

    # max_workers=1: 一次调用独占一格; 池关闭用 wait=False, 不阻塞调用方
    ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="thsdk-hard")
    try:
        fut = ex.submit(fn)
    except BaseException:
        _release_once()
        ex.shutdown(wait=False)
        raise
    try:
        result = fut.result(timeout=timeout_s)
    except FuturesTimeout:
        _incr("call_timeout")
        logger.warning("thsdk 调用硬超时(%.1fs), 返回 default; 挂死线程仍占并发槽", timeout_s)
        # 线程真正结束后再还槽 —— 否则挂死线程会堆出 SEMAPHORE 上限
        fut.add_done_callback(lambda _f: _release_once())
        ex.shutdown(wait=False)
        return default
    except swallow_exceptions as e:
        _release_once()
        ex.shutdown(wait=False)
        breaker.record_failure()
        logger.warning("thsdk 调用失败(连续 %d): %s: %s",
                       breaker._failures, type(e).__name__, str(e)[:100])
        return default
    except BaseException:
        _release_once()
        ex.shutdown(wait=False)
        raise
    _release_once()
    ex.shutdown(wait=False)
    breaker.record_success()
    return result


def thsdk_call(
    fn: Callable[[], T],
    default: Any = None,
    swallow_exceptions: tuple = (Exception,),
    *,
    timeout_s: float | None = None,
    acquire_timeout_s: float | None = None,
) -> Any:
    """thsdk 调用包装: 熔断检查 → 并发排队(acquire 超时) → 执行 → 记结果。

    Args:
        fn: thsdk 调用 lambda/函数
        default: 熔断打开、并发槽满、硬超时或异常时返回的默认值(默认 None)
        swallow_exceptions: 视为"thsdk 失败"的异常类型元组, 默认全部 Exception
        timeout_s: 硬超时秒数; 传 None 表示**不加硬超时**(在调用方自己的线程里跑,
            保持 v0.4.73 旧行为, 供已在专用线程内/自带超时护栏的调用方用)。
            传数字则走 `call_with_hard_timeout`。
        acquire_timeout_s: 并发槽等待上限; 默认 ACQUIRE_TIMEOUT_S。
    Returns:
        fn() 结果; 失败/超时/熔断中返回 default
    """
    if _breaker.is_open():
        logger.debug("thsdk 熔断中, 跳过调用(直接返回 default)")
        return default
    if timeout_s is not None:
        return call_with_hard_timeout(
            fn, default, timeout_s=timeout_s, acquire_timeout_s=acquire_timeout_s,
            swallow_exceptions=swallow_exceptions)
    try:
        with _slot(acquire_timeout_s=acquire_timeout_s):
            try:
                result = fn()
            except swallow_exceptions as e:
                _breaker.record_failure()
                logger.warning("thsdk 调用失败(连续 %d): %s: %s",
                               _breaker._failures, type(e).__name__, str(e)[:100])
                return default
    except TimeoutError as e:
        # 并发槽满 —— 不计入熔断失败(服务本身可能还好, 只是排队爆了)
        logger.warning("%s", e)
        return default
    _breaker.record_success()
    return result


def breaker_status() -> dict:
    """诊断: 返回熔断器 + 并发/硬超时计数(供健康检查/监控用)。"""
    st = _breaker.status()
    with _stats_lock:
        st.update(dict(_stats))
    st["semaphore"] = SEMAPHORE
    st["acquire_timeout_s"] = ACQUIRE_TIMEOUT_S
    st["call_timeout_s"] = CALL_TIMEOUT_S
    return st


def reset_for_tests() -> None:
    """测试用: 复位熔断器与并发计数(不碰已挂死线程, 仅重置状态/计数)。"""
    global _breaker, _sem
    _breaker = _Breaker()
    _sem = threading.BoundedSemaphore(SEMAPHORE)
    with _stats_lock:
        for k in _stats:
            _stats[k] = 0
