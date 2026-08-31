"""登录限速 — 防暴力破解(2026-08-15, 公开 demo 后登录接口暴露在公网)。

策略: 同一 IP + 用户名 5 次连续失败 → 锁 10 分钟。
内存计数 + 线程锁(单进程足够); 成功登录清零; 重启清零可接受。
"""

import threading
import time

MAX_FAILS = 5
LOCK_SECONDS = 600  # 10 分钟

# { "ip|username": {"fails": int, "locked_until": float} }
_state: dict[str, dict] = {}
_lock = threading.Lock()


def _key(ip: str, username: str) -> str:
    return f"{ip}|{username}"


def check(ip: str, username: str) -> str | None:
    """返回 None=允许尝试; str=锁定提示文案。"""
    with _lock:
        st = _state.get(_key(ip, username))
        if st and st["locked_until"] > time.time():
            remain_min = int((st["locked_until"] - time.time()) // 60) + 1
            return f"登录失败次数过多,请 {remain_min} 分钟后重试"
    return None


def fail(ip: str, username: str) -> None:
    """记录一次失败; 达到阈值则锁定。"""
    with _lock:
        k = _key(ip, username)
        st = _state.setdefault(k, {"fails": 0, "locked_until": 0})
        st["fails"] += 1
        if st["fails"] >= MAX_FAILS:
            st["locked_until"] = time.time() + LOCK_SECONDS
            st["fails"] = 0


def success(ip: str, username: str) -> None:
    """登录成功, 清零计数。"""
    with _lock:
        _state.pop(_key(ip, username), None)
