"""_MISSED_COUNTS 跨线程计数回归(2026-10-22 代码审查 Medium)。

背景: miss 计数是 read-modify-write(`d[k] = d.get(k, 0) + 1`)。GIL 只保证单条字节码
原子, **不**保证 `get` 与赋值之间不被切换线程。旧实现里 `_MISSED_COUNTS` 的读
(`missed_counts` 的 copy)、清(`_clear_state`)、增(`_on_event`)三处全部绕过 `_lock`,
而同一文件的 `_last_jsonl_write` / `_recent_occurrences` / `_last_notify` 都在锁内 ——
调度器线程 + web 线程并发下计数值会偏小, 指标失真。

这些用例走的是真实的 `_on_event` 监听回调(通过 add_listener 取出), 不是对 dict
模式的照抄, 所以能拦住"以后又把锁去掉"的回归。
"""

from __future__ import annotations

import sys
import threading

import pytest

from src.core import error_tracker as et


class _Event:
    """最小 APScheduler 事件替身: _on_event 只读 job_id / exception。"""

    def __init__(self, job_id, exc=None):
        self.job_id = job_id
        self.exception = exc


class _FakeScheduler:
    def __init__(self):
        self.fn = None

    def add_listener(self, fn, mask):
        self.fn = fn


def _listener():
    """装监听并取出真实的 _on_event 回调。"""
    sched = _FakeScheduler()
    assert et.install_scheduler_error_tracking(sched) is True
    assert sched.fn is not None
    return sched.fn


@pytest.fixture
def fast_switch():
    """把线程切换间隔压到极小, 放大 read-modify-write 的竞争窗口。

    默认 5ms 下 GIL 可能让整段递增"看起来"原子, 竞争与否取决于运气; 1µs 时
    切换几乎落在每条字节码边界上, 竞争窗口稳定被踩到。
    """
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(old)


def test_missed_counts_no_lost_update_under_concurrency(fast_switch):
    """8 线程 × 2000 次 missed 事件, 最终计数必须精确等于 16000。"""
    et._clear_state()
    fn = _listener()

    job = "klines-minute-1min"
    threads_n, per_thread = 8, 2000
    expected = threads_n * per_thread
    barrier = threading.Barrier(threads_n)

    def worker():
        barrier.wait(timeout=30)  # 尽量同时起跑, 放大竞争
        for _ in range(per_thread):
            fn(_Event(job, None))

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "并发计数超时(疑似死锁)"

    got = et.missed_counts()[job]
    assert got == expected, (
        f"并发丢计数: 期望 {expected}, 实际 {got}(丢 {expected - got} 次)"
        " —— 计数读写必须在 _lock 内完成"
    )


def test_missed_counts_returns_copy():
    """对外语义不变: 返回副本, 改副本不影响真实状态。"""
    et._clear_state()
    fn = _listener()
    fn(_Event("job-a"))

    snapshot = et.missed_counts()
    assert snapshot == {"job-a": 1}
    snapshot["job-a"] = 999
    assert et.missed_counts() == {"job-a": 1}, "missed_counts() 必须返回副本"


def test_clear_state_resets_missed_counts():
    """_clear_state() 后计数归零(测试隔离契约)。"""
    et._clear_state()
    fn = _listener()
    for _ in range(3):
        fn(_Event("nightly"))
    assert et.missed_counts() == {"nightly": 3}

    et._clear_state()
    assert et.missed_counts() == {}, "_clear_state 必须一并清空 missed 计数"


def test_missed_counts_no_deadlock_when_caller_holds_lock():
    """外层已持 _lock 时再取计数不能自锁 —— _lock 必须是可重入的。

    放在工作线程里跑: 真死锁时是 join 超时失败, 而不是把 pytest 挂死。
    """
    et._clear_state()
    fn = _listener()
    fn(_Event("job-b"))

    done = threading.Event()

    def worker():
        with et._lock:                     # 外层已持锁
            assert et.missed_counts() == {"job-b": 1}   # 内层重入取计数
        done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout=5)
    assert done.is_set(), (
        "missed_counts() 在已持 _lock 的调用路径上自锁(死锁) —— "
        "_lock 需为可重入锁(RLock)"
    )
