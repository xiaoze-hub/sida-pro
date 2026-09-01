"""WebSocket 通知 Hub (v0.4.36 P0 派活 1)。

设计 (2026-09-01):
- 多用户分发: {user_id: set[WebSocket]} 全局注册表
- 事件源触发: push_notification() 落库后, 调用 broadcast_notification() 推 WS + 累加未读
- 鉴权: 与 ws_quotes 一致 — Sec-WebSocket-Protocol 优先, ?token= 兜底
- 心跳: 30s 服务端 ping frame; 客户端任意帧回复即视作存活
- 跨线程: broadcast_notification 可被同步上下文调用, 内部用 asyncio.run_coroutine_threadsafe 投递
- Redis 累加未读: biz:notif:unread:<user_id> (HINCRBY), GET 取总数
- 跨进程: Redis Pub/Sub (biz:notif:channel:<user_id>) 兜底, 让多 worker 也都能收到

不做 (留给 v0.4.37+):
- 7 事件源 → 5 渠道回执表 (本期不实现完整矩阵, 仅复用现有 push_notification 链路)
- 推送送达状态回执 WS 推送 (用轮询 /api/notifications?since= 兜底)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Redis Pub/Sub 通道前缀, 跨 worker 兜底
_PUBSUB_CHANNEL_PREFIX = "biz:notif:channel:"
_UNREAD_KEY_PREFIX = "biz:notif:unread:"

# 同步 Redis client (独立于 async redis_client); 不可用时静默降级
_sync_redis = None
_sync_redis_lock = threading.Lock()


def _get_sync_redis():
    """获取/复用同步 Redis 客户端 (用于 PubSub / 未读计数)."""
    global _sync_redis
    if os.getenv("REDIS_DISABLED", "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    with _sync_redis_lock:
        if _sync_redis is not None:
            return _sync_redis
    try:
        import redis as redis_sync  # type: ignore
        from src.web.cache.redis_client import REDIS_URL  # noqa: F401
        client = redis_sync.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True,
            socket_connect_timeout=2.0, socket_timeout=2.0,
        )
        client.ping()
        with _sync_redis_lock:
            _sync_redis = client
        return client
    except Exception:
        return None

# WS Hub 配置 (env 可覆盖)
WS_HUB_HEARTBEAT_S = float(os.getenv("WS_HUB_HEARTBEAT_S", "30"))
WS_HUB_SEND_TIMEOUT_S = float(os.getenv("WS_HUB_SEND_TIMEOUT_S", "5"))
# 单 user 同时在线 WS 上限(防滥用)
WS_HUB_MAX_PER_USER = int(os.getenv("WS_HUB_MAX_PER_USER", "5"))
# 同 user 发送失败计数阈值(连续 N 次失败断开)
WS_HUB_SEND_FAIL_THRESHOLD = int(os.getenv("WS_HUB_SEND_FAIL_THRESHOLD", "3"))

# 用户订阅表: {user_id: {websocket: set[str(categories)}}
_subscribers: dict[str, dict[WebSocket, set[str]]] = {}
_subscribers_lock = threading.Lock()

# asyncio 主 loop 引用 (server.py 把 hub attach 到 running loop)
_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()

# 跨进程订阅者 Id (本进程内唯一自增, 用于诊断)
_sub_seq = 0


# ── Loop 绑定 ─────────────────────────────────────────────────────

def attach_event_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """server.py lifespan 启动时调用, 把 running loop 交给 hub 用于跨线程投递.

    传 None 解绑 (lifespan 关闭).
    """
    global _loop
    with _loop_lock:
        _loop = loop
    if loop:
        logger.info("[WS-Hub] 已绑定事件循环: %s", loop)
    else:
        logger.info("[WS-Hub] 事件循环已解绑")


def _get_loop() -> asyncio.AbstractEventLoop | None:
    with _loop_lock:
        return _loop


# ── 订阅管理 ─────────────────────────────────────────────────────

def _register(user_id: str, ws: WebSocket, categories: set[str]) -> int:
    global _sub_seq
    with _subscribers_lock:
        subs = _subscribers.setdefault(user_id, {})
        # 单 user 连接上限保护
        if len(subs) >= WS_HUB_MAX_PER_USER:
            logger.warning("[WS-Hub] 用户 %s 同时在线已达上限 %d, 拒绝新连接", user_id, WS_HUB_MAX_PER_USER)
            return -1
        subs[ws] = categories
        _sub_seq += 1
        sid = _sub_seq
    return sid


def _unregister(user_id: str, ws: WebSocket) -> None:
    with _subscribers_lock:
        subs = _subscribers.get(user_id)
        if subs and ws in subs:
            del subs[ws]
        if subs is not None and not subs:
            _subscribers.pop(user_id, None)


def _list_targets(user_id: str, category: str | None) -> list[tuple[WebSocket, set[str]]]:
    """拉取目标 user 的连接, 可选按订阅 category 过滤(空集合=订阅全部)."""
    with _subscribers_lock:
        subs = _subscribers.get(user_id)
        if not subs:
            return []
        items = list(subs.items())
    if not category:
        return items
    out: list[tuple[WebSocket, set[str]]] = []
    for ws, cats in items:
        # 空集合 = 订阅全部
        if not cats or category in cats:
            out.append((ws, cats))
    return out


def _all_user_ids() -> list[str]:
    with _subscribers_lock:
        return list(_subscribers.keys())


# ── 发送 (协程) ──────────────────────────────────────────────────

async def _send_one(ws: WebSocket, payload: dict, fail_counter: dict[WebSocket, int]) -> bool:
    """发送一条到单连接, 失败累加计数; 达阈值自动断开."""
    try:
        await asyncio.wait_for(ws.send_text(json.dumps(payload, ensure_ascii=False)), timeout=WS_HUB_SEND_TIMEOUT_S)
        fail_counter.pop(ws, None)
        return True
    except Exception as e:
        logger.debug("[WS-Hub] send 失败: %s", e)
        cnt = fail_counter.get(ws, 0) + 1
        fail_counter[ws] = cnt
        if cnt >= WS_HUB_SEND_FAIL_THRESHOLD:
            try:
                await ws.close(code=1011, reason="send_failed")
            except Exception:
                pass
        return False


async def _broadcast_async(user_id: str, payload: dict, category: str | None) -> int:
    """在同一 loop 内广播. 返回成功送达连接数."""
    targets = _list_targets(user_id, category)
    if not targets:
        return 0
    fail_counter: dict[WebSocket, int] = {}
    results = await asyncio.gather(
        *[_send_one(ws, payload, fail_counter) for ws, _ in targets],
        return_exceptions=True,
    )
    ok = sum(1 for r in results if r is True)
    return ok


# ── 广播入口 (跨线程安全) ─────────────────────────────────────────

def broadcast_notification(user_id: str | None, payload: dict, *, category: str | None = None) -> int:
    """同步上下文广播: 投递到主 loop, 跨进程走 Redis Pub/Sub.

    Args:
        user_id: 目标用户; None = 广播给所有 user (站内公告用)
        payload: 已序列化的 dict (含 id/title/level/category/...)
        category: 可选, 仅推送给订阅了该 category 的连接
    Returns:
        同步结果: 本进程成功送达数 (跨进程的送达在 Redis 消费侧统计)
    """
    # 1) 跨进程兜底 (Redis Pub/Sub)
    try:
        _pubsub_publish(user_id, {"payload": payload, "category": category})
    except Exception as e:
        logger.debug("[WS-Hub] PubSub publish 失败 (无 Redis 或下线): %s", e)

    # 2) 本进程直接投递
    loop = _get_loop()
    if loop is None or loop.is_closed():
        logger.debug("[WS-Hub] 无可用 loop, 仅靠 Pub/Sub 兜底")
        return 0
    fut = asyncio.run_coroutine_threadsafe(_broadcast_async(user_id or "*", payload, category), loop)
    try:
        return int(fut.result(timeout=WS_HUB_SEND_TIMEOUT_S + 2))
    except Exception as e:
        logger.debug("[WS-Hub] 跨线程广播失败: %s", e)
        return 0


def broadcast_global(payload: dict, *, category: str | None = None) -> int:
    """全 user 广播, 用于系统级公告. 返回本进程送达数."""
    user_ids = _all_user_ids()
    loop = _get_loop()
    if loop is None or loop.is_closed():
        return 0
    total = 0
    for uid in user_ids:
        fut = asyncio.run_coroutine_threadsafe(_broadcast_async(uid, payload, category), loop)
        try:
            total += int(fut.result(timeout=WS_HUB_SEND_TIMEOUT_S + 2))
        except Exception:
            pass
    return total


# ── Redis Pub/Sub 跨进程兜底 ──────────────────────────────────────

def _pubsub_publish(user_id: str | None, msg: dict) -> None:
    """发布到 Redis (biz:notif:channel:<user_id|*>) 让它们也广播到本地连接.

    用 sync redis client; 不可用时静默, 主路径不依赖.
    """
    client = _get_sync_redis()
    if client is None:
        raise RuntimeError("redis_unavailable")
    target = user_id or "*"
    client.publish(f"{_PUBSUB_CHANNEL_PREFIX}{target}", json.dumps(msg, ensure_ascii=False))


def install_pubsub_listener() -> None:
    """在 server.py lifespan 启动时注册, 把其他进程的消息也广播到本地连接.

    这是阻塞线程: 用 sync redis 的 pubsub.listen(), 收到后转本进程广播.
    """
    client = _get_sync_redis()
    if client is None:
        logger.warning("[WS-Hub] 装 PubSub 监听器失败 (无 Redis)")
        return
    pubsub = client.pubsub()
    pubsub.psubscribe(f"{_PUBSUB_CHANNEL_PREFIX}*")
    logger.info("[WS-Hub] PubSub 监听器已启动: %s*", _PUBSUB_CHANNEL_PREFIX)
    t = threading.Thread(target=_pubsub_loop, args=(pubsub,), daemon=True, name="ws-hub-pubsub")
    t.start()


def _pubsub_loop(pubsub) -> None:
    """守护线程: 消费 PubSub 消息 → 本进程广播."""
    for msg in pubsub.listen():
        try:
            if msg.get("type") not in ("pmessage", "message"):
                continue
            data = msg.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="ignore")
            obj = json.loads(data or "{}")
            inner_payload = obj.get("payload") or {}
            category = obj.get("category")
            ch = msg.get("channel", "")
            if isinstance(ch, bytes):
                ch = ch.decode("utf-8", errors="ignore")
            user_id = ch[len(_PUBSUB_CHANNEL_PREFIX):] if ch.startswith(_PUBSUB_CHANNEL_PREFIX) else None
            if user_id == "*":
                broadcast_global(inner_payload, category=category)
            elif user_id:
                broadcast_notification(user_id, inner_payload, category=category)
        except Exception as e:
            logger.debug("[WS-Hub] PubSub 消息处理失败: %s", e)


# ── 未读计数 (Redis HINCRBY) ──────────────────────────────────────

def incr_unread(user_id: str | None, n: int = 1) -> int:
    """累加未读计数. user_id=None 时不动 Redis (站内公告不入未读)."""
    if not user_id:
        return 0
    client = _get_sync_redis()
    if client is None:
        return 0
    try:
        return int(client.hincrby(f"{_UNREAD_KEY_PREFIX}{user_id}", "count", n))
    except Exception:
        return 0


def get_unread(user_id: str | None) -> int:
    if not user_id:
        return 0
    client = _get_sync_redis()
    if client is None:
        return 0
    try:
        v = client.hget(f"{_UNREAD_KEY_PREFIX}{user_id}", "count")
        return int(v) if v else 0
    except Exception:
        return 0


def reset_unread(user_id: str | None) -> None:
    if not user_id:
        return
    client = _get_sync_redis()
    if client is None:
        return
    try:
        client.delete(f"{_UNREAD_KEY_PREFIX}{user_id}")
    except Exception:
        pass


# ── WS 端点处理 (被 ws_notifications.py 调用) ─────────────────────

async def ws_notifications_handler(websocket: WebSocket) -> None:
    """WS 端点: 鉴权 → 注册 → 接收 subscribe/ack → 持续心跳 + 推送.

    客户端协议 (JSON 帧):
      客户端 → 服务端:
        {"type":"subscribe","categories":["agent_run","alert"]}
        {"type":"ack"}                心跳应答 (服务端 ping 帧后任意帧即视作 alive)
        {"type":"reset_unread"}       重置未读 (打开通知中心时调用)
      服务端 → 客户端:
        {"type":"hello","user_id":"...","ts":...}
        {"type":"event", ...payload}  推送一条事件(原样转发 push_notification 的入参 + id)
        {"type":"ping"}               心跳 (客户端无需回复, 任意帧即可)
    """
    from src.web.api.auth import decode_token  # 延迟 import, 避免循环

    # 1) 鉴权 (复用 ws_quotes 模式)
    token, swp_sub = await _extract_ws_token(websocket)
    payload = decode_token(token) if token else None
    if not payload or not payload.get("sub"):
        try:
            await websocket.close(code=4401, reason="unauthorized")
        except Exception:
            pass
        return
    user_id = str(payload["sub"])

    if swp_sub:
        await websocket.accept(subprotocol=swp_sub)
    else:
        await websocket.accept()

    # 2) 注册订阅 (categories 由首帧 subscribe 设置, 启动先按"全部"接收)
    sid = _register(user_id, websocket, set())
    if sid < 0:
        try:
            await websocket.close(code=4402, reason="too_many_connections")
        except Exception:
            pass
        return

    # 3) hello 帧 (客户端据此确认连接 + 同步未读)
    try:
        await websocket.send_text(json.dumps({
            "type": "hello",
            "user_id": user_id,
            "unread": get_unread(user_id),
            "ts": int(time.time()),
        }, ensure_ascii=False))
    except Exception:
        _unregister(user_id, websocket)
        return

    fail_counter: dict[WebSocket, int] = {}
    try:
        while True:
            # 并行: 收客户端消息 OR 心跳计时
            recv_task = asyncio.create_task(websocket.receive_text())
            ping_task = asyncio.create_task(asyncio.sleep(WS_HUB_HEARTBEAT_S))
            done, pending = await asyncio.wait(
                {recv_task, ping_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

            if recv_task in done:
                try:
                    raw_text = recv_task.result()
                except Exception:
                    # 客户端断开
                    return
                try:
                    obj = json.loads(raw_text)
                except Exception:
                    continue
                mtype = obj.get("type")
                if mtype == "subscribe":
                    cats = obj.get("categories") or []
                    if isinstance(cats, list):
                        with _subscribers_lock:
                            subs = _subscribers.get(user_id)
                            if subs and websocket in subs:
                                subs[websocket] = {str(c) for c in cats if c}
                elif mtype == "reset_unread":
                    reset_unread(user_id)
                    await _send_one(websocket, {"type": "unread_reset"}, fail_counter)
                elif mtype == "ack" or mtype == "pong":
                    pass
                # 其他任意帧视作 alive (心跳应答)
            if ping_task in done:
                if not await _send_one(websocket, {"type": "ping", "ts": int(time.time())}, fail_counter):
                    return
    except Exception as e:
        logger.debug("[WS-Hub] ws 循环异常: %s", e)
    finally:
        _unregister(user_id, websocket)


async def _extract_ws_token(websocket: WebSocket) -> tuple[str, str]:
    """复用 ws_quotes 的 token 提取模式 (Sec-WebSocket-Protocol > ?token=)."""
    swp = websocket.headers.get("sec-websocket-protocol", "")
    if swp:
        parts = [p.strip() for p in swp.split(",") if p.strip()]
        for i, p in enumerate(parts):
            if p == "panwatch.auth.bearer" and i + 1 < len(parts):
                return parts[i + 1], ("panwatch.auth.bearer" if "panwatch.auth.bearer" in parts else parts[0])
        if parts:
            return parts[-1], parts[0]
    return websocket.query_params.get("token", ""), ""


# ── 诊断 ─────────────────────────────────────────────────────────

def stats() -> dict[str, Any]:
    with _subscribers_lock:
        total_users = len(_subscribers)
        total_conns = sum(len(v) for v in _subscribers.values())
        per_user = {uid: len(ws_set) for uid, ws_set in _subscribers.items()}
    loop = _get_loop()
    return {
        "users": total_users,
        "connections": total_conns,
        "per_user": per_user,
        "loop_attached": loop is not None and not loop.is_closed(),
    }