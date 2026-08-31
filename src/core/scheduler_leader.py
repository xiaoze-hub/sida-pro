"""调度器选主(2026-08-23 Q1): 多 uvicorn worker 下防止定时任务双跑。

背景: WEB_WORKERS=2 时每个 worker 的 lifespan 都会启动全部 APScheduler +
微信长轮询 → 定时 Agent 跑两遍(LLM 费用翻倍/通知重复)、撮合双份触发。

机制: Redis 租约 SET NX EX 30, 每 10s 续期; 拿不到租约的 worker 不启调度器。
- SIDA_ENABLE_SCHEDULERS=1/0 可强制(本地调试用)
- Redis 不可用 → 回退旧行为(启动)并告警
- 启动时最多重试 40s(dev reload 旧进程锁残留 30s 内自然过期, 不会误杀)
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time

logger = logging.getLogger(__name__)

LOCK_KEY = "sida:scheduler_leader"
TTL_SECONDS = 30
RENEW_INTERVAL = 10
ACQUIRE_RETRY_SECONDS = 40

_holder: dict = {"id": ""}
_acquired: bool = False  # 本 worker 是否成为调度器租约持有者(或回退运行)


def is_leader() -> bool:
    """本进程是否持有调度器职责(供 health 探针判断非 leader 是预期还是故障)。"""
    return _acquired


def _client():
    import redis

    return redis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def try_acquire() -> tuple[bool, str]:
    """决定本 worker 是否启动调度器。返回 (是否启动, 原因说明)。"""
    global _acquired
    force = (os.getenv("SIDA_ENABLE_SCHEDULERS") or "").strip().lower()
    if force == "0":
        _acquired = False
        return False, "SIDA_ENABLE_SCHEDULERS=0"
    if force == "1":
        _acquired = True
        return True, "SIDA_ENABLE_SCHEDULERS=1 强制启动"

    wid = worker_id()
    deadline = time.time() + ACQUIRE_RETRY_SECONDS
    last_note = ""
    while True:
        try:
            r = _client()
            got = r.set(LOCK_KEY, wid, nx=True, ex=TTL_SECONDS)
            if got:
                _start_renewal(r, wid)
                _acquired = True
                return True, "Redis 选主成功"
            current = r.get(LOCK_KEY)
            current_owner = current.decode("utf-8", "replace") if current else "?"
            if current_owner == wid:
                r.expire(LOCK_KEY, TTL_SECONDS)
                _start_renewal(r, wid)
                _acquired = True
                return True, "Redis 选主续期(reload 重入)"
            last_note = f"leader={current_owner}"
        except Exception as e:
            # Redis 不可用: 保持旧行为(启动), 避免单机部署因 Redis 故障丢调度
            logger.warning(
                "[选主] Redis 不可用(%s), 回退为本 worker 启动调度器"
                "(多 worker 部署下可能重复执行, 请修复 Redis)", e,
            )
            _acquired = True
            return True, "Redis 不可用回退(旧行为)"
        if time.time() >= deadline:
            _acquired = False
            return False, f"选主失败({last_note}), 本 worker 让位"
        time.sleep(5)


def _start_renewal(r, wid: str) -> None:
    def loop():
        while _holder["id"] == wid:
            time.sleep(RENEW_INTERVAL)
            try:
                cur = r.get(LOCK_KEY)
                if cur and cur.decode("utf-8", "replace") == wid:
                    r.expire(LOCK_KEY, TTL_SECONDS)
                else:
                    logger.warning("[选主] 租约丢失, 停止续期(调度器运行至进程重启)")
                    return
            except Exception as e:
                logger.warning("[选主] 续期失败(%s), 租约将过期, 其他 worker 可接管", e)
                return

    _holder["id"] = wid
    threading.Thread(target=loop, daemon=True, name="scheduler-leader-renewal").start()
