"""失败日志限流(2026-10-08 可观测性审计 P2)。

背景: Engine/vendor 取数失败时**每条一 warning** —— TQ 断链时全市场分片/多源链
会瞬间刷出成百上千行相同告警, 把真正有用的日志淹没, 运维看不到关键信息。

策略: 按 (provider, kind) 维度限流, 同一 key 每 ``window``(默认 60s)最多打一条;
窗口内被抑制的次数在下次真正打印时以 "(窗口内另有 N 次同类失败)" 附加说明 ——
既不丢信息(次数可见), 又不刷屏。明细仍走 datasource_failures 表(自带 60s 限流)。

纯进程内状态, 线程安全; 测试可传 ``now`` 控制时钟, 或调 ``reset()`` 隔离。
"""

from __future__ import annotations

import logging
import threading
import time

DEFAULT_WINDOW_SEC = 60.0

_lock = threading.Lock()
# key → (上次真正打印的 monotonic 时间, 自那之后被抑制的次数)
_state: dict[str, tuple[float, int]] = {}


def should_log(key: str, *, window: float = DEFAULT_WINDOW_SEC,
               now: float | None = None) -> tuple[bool, int]:
    """该 key 是否应真正打印; 返回 (是否打印, 自上次打印以来被抑制的次数)。

    - 首次 / 距上次打印 >= window → 打印, 抑制计数归零。
    - 窗口内再次调用 → 不打印, 抑制计数 +1。
    """
    now = time.monotonic() if now is None else now
    k = str(key)
    with _lock:
        last, suppressed = _state.get(k, (0.0, 0))
        if last and now - last < window:
            _state[k] = (last, suppressed + 1)
            return False, suppressed + 1
        _state[k] = (now, 0)
        return True, 0


def log_failure(
    logger: logging.Logger,
    key: str,
    msg: str,
    *,
    level: int = logging.WARNING,
    window: float = DEFAULT_WINDOW_SEC,
) -> bool:
    """限流打印一条失败日志; 返回是否真的打印了(未打印即被抑制)。"""
    emit, suppressed = should_log(key, window=window)
    if not emit:
        return False
    if suppressed:
        msg = f"{msg} (窗口内另有 {suppressed} 次同类失败被抑制)"
    logger.log(level, msg)
    return True


def reset() -> None:
    """测试隔离用: 清空限流窗口。"""
    with _lock:
        _state.clear()
