"""只读重计算的跨进程互斥门(2026-11-12 复审)。

**问题**: `GET /api/factors/calibration` 每轮都要扫最近 N 天快照 + 回填持有期,
是不轻的只读查询。原来的保护(single-flight / 最小间隔 / 结果缓存)全是**进程内**
状态, 而生产 `WEB_WORKERS` 默认 4(`src/bootstrap/cli.py`)—— 4 个 worker 各自
单飞, 真实并发是 4 轮而不是 1 轮。把「进程内 single-flight」当「全局最多一轮」
对外宣称是假的。

**本模块只做一件事**: 用一个跨进程租约(Redis `SET NX EX`)把实际计算串起来,
外加一个跨进程最小间隔键, 让「换参数扫一轮」也受全局节流。

取舍(为什么是 fail-open):
- 只读计算的互斥只影响资源占用, **不影响正确性**。Redis 抖动时宁可多跑一轮,
  也不该让面板 500 或长时间 429 —— 所以不可用时**降级放行**, 并如实返回
  `scope="process"`, 让调用方把「本进程单飞」而不是「全局单飞」告诉用户。
- 复用已有 Redis 依赖与连接参数(同步客户端 + 短超时 + 失败冷却), 不新增服务、
  不新增第三方依赖。`REDIS_DISABLED=1` / `READONLY_GATE_DISABLED=1` 可显式关掉。

**局限(必须原样告知调用方, 不得美化)**:
- 结果缓存仍是**每 worker 一份**(进程内 L1), 所以 cluster 门只保证「同一时刻
  最多一轮实际计算 / 最小间隔内不重复计算」, **不保证**各 worker 复用同一份结果。
- Redis 不可用时退化为进程内单飞, 等价于改造前的行为(不是回退到无保护)。
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: 门闩的粒度: "cluster" = Redis 租约生效(跨 worker 互斥);
#: "process" = Redis 不可用, 只有本进程内的 single-flight(不对外宣称全局)。
GATE_SCOPE_CLUSTER = "cluster"
GATE_SCOPE_PROCESS = "process"

_KEY_PREFIX = "sida:readonly_gate:"
#: 租约硬上限(秒): 进程被杀也不会永久占锁; 正常路径在 finally 里主动释放。
MAX_LEASE_SECONDS = 120.0

#: Redis 连接/操作超时(秒): 只读计算的互斥不值得让请求等太久。
_CONNECT_TIMEOUT = 0.5
_OP_TIMEOUT = 1.0
#: 连接失败后的冷却(秒): 冷却窗口内不再尝试, 直接走进程内门闩, 避免每请求撞超时。
_RETRY_AFTER_SEC = 30.0

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

_client = None
_client_attempted_at = 0.0


@dataclass(frozen=True)
class GateTicket:
    """一次门闩的凭证。

    - `held=True` 才能去计算; `held=False` 说明别的 worker 正在算或被节流。
    - `scope` 是**这次判定生效的范围**: cluster=跨 worker, process=仅本进程。
    - `reason`: busy(别的 worker 在算) / throttled(全局最小间隔内) /
      redis_unavailable(降级) / ""(正常持有或不可用信息缺失)。
    """

    held: bool
    scope: str
    token: str
    reason: str = ""

    @property
    def cluster_wide(self) -> bool:
        return self.scope == GATE_SCOPE_CLUSTER


def _enabled() -> bool:
    for name in ("REDIS_DISABLED", "READONLY_GATE_DISABLED"):
        if (os.getenv(name, "") or "").strip().lower() in ("1", "true", "yes", "on"):
            return False
    return True


def _redis():
    """同步 Redis 客户端; 不可用返回 None(带冷却, 不抛异常)。

    为什么不复用 `src/db/redis_client`: 那是 asyncio 客户端(跨事件循环会报
    "attached to a different loop"), 而标定端点是同步函数。与
    `src/core/md_redis_cache.py` 同一条折衷路线(同步客户端 + 短超时 + 冷却)。
    """
    global _client, _client_attempted_at

    if not _enabled():
        return None
    if _client is not None:
        return _client
    now = time.monotonic()
    if _client_attempted_at and now - _client_attempted_at < _RETRY_AFTER_SEC:
        return None
    _client_attempted_at = now
    try:
        import redis

        cli = redis.Redis.from_url(
            os.getenv("REDIS_URL", "redis://localhost:6379/0").strip(),
            socket_connect_timeout=_CONNECT_TIMEOUT,
            socket_timeout=_OP_TIMEOUT,
        )
        cli.ping()
        _client = cli
        return cli
    except Exception as e:  # noqa: BLE001 - Redis 不可用属于常态, 不是错误路径
        logger.debug("[readonly-gate] Redis 不可用, 退回进程内单飞: %s", e)
        _client = None
        return None


def reset_client() -> None:
    """丢弃缓存的 Redis 客户端/冷却状态(测试与运维热重启用, 不加 HTTP 入口)。"""
    global _client, _client_attempted_at
    _client = None
    _client_attempted_at = 0.0


def _key(name: str, suffix: str) -> str:
    return f"{_KEY_PREFIX}{name}:{suffix}"


def _new_token(name: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{name}:{uuid.uuid4().hex}"


def acquire(name: str, *, ttl_seconds: float = MAX_LEASE_SECONDS,
            min_interval_seconds: float = 0.0) -> GateTicket:
    """尝试取得 `name` 的跨进程门闩(拿不到不要重试, 直接回 429/降级说明)。

    - Redis 不可用 → `held=True, scope="process"`: 放行本进程, 但明确不是全局。
    - 别的 worker 正在算 → `held=False, scope="cluster", reason="busy"`。
    - 距上一次计算(任何 worker)太近 → `held=False, reason="throttled"`。
    """
    token = _new_token(name)
    cli = _redis()
    if cli is None:
        return GateTicket(held=True, scope=GATE_SCOPE_PROCESS, token=token,
                          reason="redis_unavailable")

    lease = max(1, int(min(float(ttl_seconds), MAX_LEASE_SECONDS)))
    gate_key = _key(name, "gate")
    try:
        if not cli.set(gate_key, token, nx=True, ex=lease):
            return GateTicket(held=False, scope=GATE_SCOPE_CLUSTER, token=token,
                              reason="busy")
        interval = float(min_interval_seconds or 0.0)
        if interval > 0:
            # 跨进程最小间隔: 任何 worker 刚算过就不许再开一轮(换参数也一样)。
            px = max(1, int(interval * 1000))
            if not cli.set(_key(name, "ts"), token, nx=True, px=px):
                _release_key(cli, gate_key, token)
                return GateTicket(held=False, scope=GATE_SCOPE_CLUSTER,
                                  token=token, reason="throttled")
        return GateTicket(held=True, scope=GATE_SCOPE_CLUSTER, token=token)
    except Exception as e:  # noqa: BLE001 - 门闩失败不影响只读计算的正确性
        logger.warning("[readonly-gate] 门闩操作失败, 本轮降级为进程内单飞: %s", e)
        return GateTicket(held=True, scope=GATE_SCOPE_PROCESS, token=token,
                          reason="redis_unavailable")


def release(ticket: GateTicket | None, name: str) -> None:
    """释放门闩(只删自己那把); 任何异常都吞掉, 不影响响应。

    客户端用 `_redis()` 现取: acquire 之后 Redis 若掉线, 这里拿到 None 就跳过,
    由租约 TTL 兜底(最多多堵 `MAX_LEASE_SECONDS`)。
    """
    if ticket is None or ticket.scope != GATE_SCOPE_CLUSTER:
        return
    cli = _redis()
    if cli is None:
        return
    _release_key(cli, _key(name, "gate"), ticket.token)


def _release_key(cli, key: str, token: str) -> None:
    """compare-and-delete: 只删自己的租约, 不误删别人的。

    Lua 不可用(极老/受限的 Redis 兼容层)时退化为 GET+DEL; 这里的竞态最坏
    结果是多放行一轮只读计算, 不影响正确性。
    """
    try:
        cli.eval(_RELEASE_LUA, 1, key, token)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        if cli.get(key) == token:
            cli.delete(key)
    except Exception as e:  # noqa: BLE001
        logger.debug("[readonly-gate] 释放门闩失败(%s), 交给 TTL 兜底: %s", key, e)
