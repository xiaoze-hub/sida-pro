"""调度器选主(2026-08-23 Q1; 2026-09-08 风险方案1.3/A3 改 fail-closed + 租约丢失真停)。

背景: WEB_WORKERS=2 时每个 worker 的 lifespan 都会启动全部 APScheduler +
微信长轮询 → 定时 Agent 跑两遍(LLM 费用翻倍/通知重复)、撮合双份触发。

机制: Redis 租约 SET NX EX 30, 每 10s 续期; 拿不到租约的 worker 不启调度器。
- SIDA_ENABLE_SCHEDULERS=0 强制不启; =1 强制启动(显式口子, 兼容旧部署)
- SIDA_SCHEDULER_SINGLE_INSTANCE=1 单实例部署显式跳过选主(开发环境口子)
- Redis 不可用 → **fail-closed**: 不启动调度器(旧逻辑"回退全员启动"会让
  所有实例自认 leader, 重复推送/重复写库/重复烧 LLM 钱), 指数退避重试探测,
  Redis 恢复后自动选主
- 租约丢失/续期失败 → scheduler_registry 全部 **pause(真停)**(旧逻辑只打日志,
  调度器跑到进程重启, 选主形同虚设), 重新取得租约后自动 resume

红线例外说明: 此处独立裸连 Redis 而不走 biz_cache —— 选主是分布式锁语义
(NX/EX/续期精确控制), 不是业务缓存; biz_cache 的写路径不含 NX 语义。
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

_acquired: bool = False  # 本 worker 是否成为调度器租约持有者(或单实例口子放行)
_state: str = "init"  # leader / standby / failed / init(供 /health 区分"没当上"与"没敢当")


def leader_state() -> str:
    """三态: leader=本实例持锁; standby=合法让位(别的实例是 leader);
    failed=选主失败/Redis 不可用(fail-closed, 无任何实例在跑调度)。"""
    return _state


def _client():
    import redis

    return redis.Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _env_on(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() == "1"


def _become_leader() -> None:
    global _acquired, _state
    _acquired = True
    _state = "leader"


def try_acquire() -> tuple[bool, str]:
    """决定本 worker 是否启动调度器。返回 (是否启动, 原因说明)。"""
    global _acquired, _state
    force = (os.getenv("SIDA_ENABLE_SCHEDULERS") or "").strip().lower()
    if force == "0":
        _acquired = False
        _state = "standby"
        return False, "SIDA_ENABLE_SCHEDULERS=0"
    if force == "1":
        _become_leader()
        return True, "SIDA_ENABLE_SCHEDULERS=1 强制启动"
    if _env_on("SIDA_SCHEDULER_SINGLE_INSTANCE"):
        _become_leader()
        return True, "SIDA_SCHEDULER_SINGLE_INSTANCE=1 单实例跳过选主"

    wid = worker_id()
    deadline = time.time() + ACQUIRE_RETRY_SECONDS
    backoff = 2.0
    last_note = ""
    redis_down = False
    while True:
        redis_down = False
        try:
            r = _client()
            got = r.set(LOCK_KEY, wid, nx=True, ex=TTL_SECONDS)
            if got:
                _start_renewal(r, wid)
                _become_leader()
                return True, "Redis 选主成功"
            current = r.get(LOCK_KEY)
            current_owner = current.decode("utf-8", "replace") if current else "?"
            if current_owner == wid:
                r.expire(LOCK_KEY, TTL_SECONDS)
                _start_renewal(r, wid)
                _become_leader()
                return True, "Redis 选主续期(reload 重入)"
            last_note = f"leader={current_owner}"
            # 锁在别人手里: 继续等最多 40s —— 覆盖 dev reload 场景(旧进程的
            # 残留锁 30s 内自然过期, 新进程可接管); 对方活着则其持续续期,
            # 到期让位(standby, 合法状态)。
        except Exception as e:
            # A3 fail-closed: Redis 不可用绝不自认 leader。
            # (旧逻辑此处直接"回退为本 worker 启动" → 所有实例并发双跑)
            redis_down = True
            logger.error(
                "[选主] Redis 不可用(%s), fail-closed 不启动调度器, 指数退避重试", e,
            )
        if time.time() >= deadline:
            _acquired = False
            _state = "standby" if last_note else "failed"
            return False, (f"选主失败({last_note}), 本 worker 让位" if last_note
                           else "Redis 不可用, 保守放弃 leader(fail-closed)")
        if redis_down:
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
        else:
            time.sleep(5)


def _pause_all() -> None:
    """真停本实例全部已注册调度器(风险方案1.3: 租约丢失不再"运行至进程重启")。"""
    from src.core import scheduler_registry

    for name, sched in scheduler_registry.get_all().items():
        try:
            if hasattr(sched, "pause"):
                sched.pause()
        except Exception as e:  # noqa: BLE001
            logger.warning("[选主] 暂停调度器 %s 失败: %s", name, e)


def _resume_all() -> None:
    from src.core import scheduler_registry

    for name, sched in scheduler_registry.get_all().items():
        try:
            if hasattr(sched, "resume"):
                sched.resume()
        except Exception as e:  # noqa: BLE001
            logger.warning("[选主] 恢复调度器 %s 失败: %s", name, e)


def _start_renewal(r, wid: str) -> None:
    """续期守护线程: 正常续租; 租约被抢/Redis 不可达 → 真停 + 自动重选。"""
    def loop():
        paused = False
        while True:
            time.sleep(RENEW_INTERVAL)
            paused = _renewal_once(r, wid, paused)

    threading.Thread(target=loop, daemon=True, name="scheduler-leader-renewal").start()


def _renewal_once(r, wid: str, paused: bool) -> bool:
    """续期一轮, 返回新的 paused 状态(独立成函数便于测试, 不依赖 10s 线程)。

    - 仍持有租约: 续期, 此前真停过则 resume;
    - 租约被抢/过期: 真停 + 立即 NX 抢回(成功即 resume);
    - Redis 不可达: 保守真停, 恢复后下一轮自动续。
    """
    try:
        cur = r.get(LOCK_KEY)
        if cur and cur.decode("utf-8", "replace") == wid:
            r.expire(LOCK_KEY, TTL_SECONDS)
            if paused:
                logger.warning("[选主] 租约恢复, resume 本实例调度器")
                _resume_all()
            return False
        owner = cur.decode("utf-8", "replace") if cur else "(键已过期)"
        if not paused:
            logger.error(
                "[选主] 租约丢失(现持有者=%s), 真停本实例全部调度器", owner,
            )
            _pause_all()
        # 立即尝试抢回: 键已过期且无人接手时本实例续任
        if r.set(LOCK_KEY, wid, nx=True, ex=TTL_SECONDS):
            logger.warning("[选主] 重新取得租约, resume 本实例调度器")
            _resume_all()
            return False
        return True
    except Exception as e:
        # Redis 不可达: 无法续租 → 租约必然过期, 保守真停。
        if not paused:
            logger.error("[选主] 续期失败(%s), 保守暂停本实例全部调度器(真停)", e)
            _pause_all()
        return True
