"""统一实时信封(2026-09-07 P2 成熟化): WS 下行全部走 envelope, Hub 抽独立进程的前置条件。

帧格式: {"seq": int, "ts": float, "topic": str, "user_id": str, "payload": dict}
- topic: quote.tick / quote.snapshot / notif.push(notifyhub 原样 payload)
- seq: Redis INCR biz:ws:seq 全局单调; 无 Redis 退进程内计数(多 worker 下仅本进程单调)
- ring: 本进程 deque(200) 供 ?last_seq= 断线重放(best-effort; 跨进程重放是 Hub 独立后的事)

前端断线重连: ws://host/api/quotes/ws?token=<jwt>&last_seq=<上次最大seq>
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

_SEQ_KEY = "biz:ws:seq"
_RING_MAX = 200

_lock = threading.Lock()
_local_seq = 0
_ring: deque = deque(maxlen=_RING_MAX)


_seq_client: Any = None
_seq_client_failed = False


def _get_seq_client() -> Any:
    """Redis 客户端单例(2026-09-08 审计修复: 原实现在 _next_seq 内每次 from_url 新建连接,
    每帧 WS 推送都建连, 高频推送下连接风暴; 对齐 ws_hub.py 的单例缓存写法)。"""
    global _seq_client, _seq_client_failed
    if _seq_client is not None:
        return _seq_client
    if _seq_client_failed:
        raise RuntimeError("redis_unavailable")
    import os

    if os.getenv("REDIS_DISABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        raise RuntimeError("redis_disabled")
    import redis as redis_sync  # type: ignore
    from src.web.cache.redis_client import REDIS_URL

    client = redis_sync.from_url(REDIS_URL, encoding="utf-8", decode_responses=True,
                                 socket_connect_timeout=1.0, socket_timeout=1.0)
    client.ping()
    _seq_client = client
    return _seq_client


def _drop_seq_client() -> None:
    """连接异常时置空, 下次取 seq 重建(简单自愈)。"""
    global _seq_client, _seq_client_failed
    _seq_client = None
    _seq_client_failed = True


def _next_seq() -> int:
    """全局单调 seq(Redis INCR, 失败退进程内)。"""
    global _local_seq
    try:
        client = _get_seq_client()
        return int(client.incr(_SEQ_KEY))
    except Exception:
        _drop_seq_client()
        with _lock:
            _local_seq += 1
            return _local_seq


def pack(topic: str, user_id: str | None, payload: dict) -> dict:
    """打包一帧并记入重放 ring。payload 必须是 JSON 可序列化 dict。"""
    env = {
        "seq": _next_seq(),
        "ts": time.time(),
        "topic": topic,
        "user_id": user_id or "*",
        "payload": payload if isinstance(payload, dict) else {"data": payload},
    }
    with _lock:
        _ring.append(env)
    return env


def replay_since(last_seq: int | str | None, user_id: str | None = None) -> list[dict]:
    """返回 ring 中 seq>last_seq 的帧(按 seq 升序); user_id 非空时只回属于它的帧 + 全播帧。"""
    try:
        base = int(last_seq or 0)
    except (TypeError, ValueError):
        base = 0
    if base <= 0:
        return []
    with _lock:
        frames = list(_ring)
    out = [f for f in frames if f.get("seq", 0) > base]
    if user_id:
        out = [f for f in out if f.get("user_id") in (user_id, "*")]
    return sorted(out, key=lambda f: f.get("seq", 0))


def reset_for_tests() -> None:
    """测试隔离: 清空 ring、进程内 seq 与 Redis 单例。"""
    global _local_seq, _seq_client, _seq_client_failed
    with _lock:
        _ring.clear()
        _local_seq = 0
        _seq_client = None
        _seq_client_failed = False


def ring_depth() -> int:
    with _lock:
        return len(_ring)
