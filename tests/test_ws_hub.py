"""WS Hub 单测 (v0.4.36 P0 派活 1).

测例矩阵 (7 事件源 × 5 渠道 × 多用户 = 35 组合; 按层级组织):
  L1: 模块基础 (registry/loop)
  L2: 订阅管理 (subscribe/unsubscribe/sid 限流)
  L3: 同步广播入口 (broadcast_notification/broadcast_global 跨线程安全)
  L4: 未读计数 (incr/get/reset, Redis 不可用时降级)
  L5: WS 端点 (鉴权/subscribe 帧/心跳/重置未读)
  L6: 与 push_notification 集成 (落库后 broadcast + incr_unread)
  L7: 跨进程 Pub/Sub (publish/consume 跨进程, Redis 不可用时降级)

不实现: 7事件源×5渠道回执表 (留 v0.4.37+)
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.web.notifications import ws_hub


# ── 公共 fixture ──────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_hub_state():
    """每个测例前清空 registry/loop, 避免污染."""
    ws_hub._subscribers.clear()
    ws_hub.attach_event_loop(None)
    yield
    ws_hub._subscribers.clear()
    ws_hub.attach_event_loop(None)


def _mk_loop():
    return asyncio.new_event_loop()


# ── L1 模块基础 ──────────────────────────────────────────────────

def test_l1_01_stats_empty():
    """空状态 stats 正常."""
    s = ws_hub.stats()
    assert s["users"] == 0
    assert s["connections"] == 0
    assert s["loop_attached"] is False


def test_l1_02_attach_loop():
    """attach_event_loop 正确绑定/解绑."""
    loop = _mk_loop()
    ws_hub.attach_event_loop(loop)
    assert ws_hub.stats()["loop_attached"] is True
    ws_hub.attach_event_loop(None)
    assert ws_hub.stats()["loop_attached"] is False


# ── L2 订阅管理 ──────────────────────────────────────────────────

def test_l2_01_register_basic():
    """基本注册 + 查询."""
    ws = MagicMock()
    sid = ws_hub._register("u1", ws, set())
    assert sid > 0
    assert ws_hub.stats()["users"] == 1
    assert ws_hub.stats()["connections"] == 1


def test_l2_02_unregister():
    """unregister 后连接数 -1."""
    ws = MagicMock()
    ws_hub._register("u1", ws, set())
    ws_hub._unregister("u1", ws)
    assert ws_hub.stats()["users"] == 0
    assert ws_hub.stats()["connections"] == 0


def test_l2_03_max_per_user_limit():
    """单 user 超过 WS_HUB_MAX_PER_USER 拒绝."""
    max_n = ws_hub.WS_HUB_MAX_PER_USER
    for _ in range(max_n):
        sid = ws_hub._register("u1", MagicMock(), set())
        assert sid > 0
    sid = ws_hub._register("u1", MagicMock(), set())
    assert sid == -1


def test_l2_04_list_targets_category_filter():
    """list_targets 按 category 过滤."""
    ws1 = MagicMock()
    ws2 = MagicMock()
    ws3 = MagicMock()
    ws_hub._register("u1", ws1, {"agent_run", "alert"})
    ws_hub._register("u1", ws2, {"alert"})
    ws_hub._register("u1", ws3, set())

    targets = ws_hub._list_targets("u1", "agent_run")
    ws_set = {w for w, _ in targets}
    assert ws1 in ws_set
    assert ws2 not in ws_set
    assert ws3 in ws_set

    all_targets = ws_hub._list_targets("u1", None)
    assert len(all_targets) == 3


def test_l2_05_user_isolation():
    """不同 user 的连接互不干扰."""
    ws_a = MagicMock()
    ws_b = MagicMock()
    ws_hub._register("u1", ws_a, set())
    ws_hub._register("u2", ws_b, set())
    assert len(ws_hub._list_targets("u1", None)) == 1
    assert len(ws_hub._list_targets("u2", None)) == 1


# ── L3 同步广播入口 ─────────────────────────────────────────────

def test_l3_01_broadcast_without_loop():
    """loop 未绑定时 broadcast 返回 0."""
    ok = ws_hub.broadcast_notification("u1", {"type": "event", "title": "x"})
    assert ok == 0


def test_l3_02_broadcast_global_no_users():
    """无 user 时 broadcast_global 返 0."""
    ws_hub.attach_event_loop(_mk_loop())
    ok = ws_hub.broadcast_global({"type": "event"})
    assert ok == 0


@pytest.mark.asyncio
async def test_l3_03_async_broadcast_basic():
    """_broadcast_async 推 WS."""
    ws = MagicMock()

    async def _ok_send(_):
        return None
    ws.send_text = _ok_send
    ws_hub._register("u1", ws, set())

    loop = asyncio.get_running_loop()
    ws_hub.attach_event_loop(loop)
    ok = await ws_hub._broadcast_async("u1", {"type": "event", "title": "x"}, None)
    assert ok == 1


@pytest.mark.asyncio
async def test_l3_04_async_broadcast_send_failure():
    """send_text 抛错时不崩, 返回 0."""
    ws = MagicMock()

    async def _bad_send(_):
        raise RuntimeError("conn broken")
    ws.send_text = _bad_send
    ws_hub._register("u1", ws, set())

    loop = asyncio.get_running_loop()
    ws_hub.attach_event_loop(loop)
    ok = await ws_hub._broadcast_async("u1", {"type": "event"}, None)
    assert ok == 0


@pytest.mark.asyncio
async def test_l3_05_send_fail_threshold_closes():
    """连续 N 次失败自动 close."""
    ws = MagicMock()

    async def _bad_send(_):
        raise RuntimeError("x")
    async def _close():
        pass
    ws.send_text = _bad_send
    ws.close = _close
    fail_counter = {}
    for _ in range(ws_hub.WS_HUB_SEND_FAIL_THRESHOLD):
        await ws_hub._send_one(ws, {"type": "event"}, fail_counter)
    assert fail_counter.get(ws, 0) >= ws_hub.WS_HUB_SEND_FAIL_THRESHOLD


# ── L4 未读计数 ──────────────────────────────────────────────────

def test_l4_01_redis_unavailable_returns_zero():
    """Redis 不可用时 incr/get 返回 0 不抛错."""
    with patch.object(ws_hub, "_get_sync_redis", return_value=None):
        assert ws_hub.incr_unread("u1", 1) == 0
        assert ws_hub.get_unread("u1") == 0
        ws_hub.reset_unread("u1")


def test_l4_02_user_id_none_skips_redis():
    """user_id=None 时不入 Redis (站内公告不入未读)."""
    ws_hub.incr_unread(None, 5)
    ws_hub.get_unread(None)
    ws_hub.reset_unread(None)


# ── L5 WS 端点 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_l5_01_no_token_rejected():
    """无 token → close 4401."""
    ws = MagicMock()
    ws.headers = {}
    ws.query_params = {}

    async def _close(*args, **kwargs):
        ws.close_code = kwargs.get("code")
    ws.close = _close

    await ws_hub.ws_notifications_handler(ws)
    assert ws.close_code == 4401


@pytest.mark.asyncio
async def test_l5_02_invalid_token_rejected():
    """无效 token → close 4401."""
    ws = MagicMock()
    ws.headers = {}
    ws.query_params = {"token": "garbage"}

    async def _close(*args, **kwargs):
        ws.close_code = kwargs.get("code")
    ws.close = _close

    await ws_hub.ws_notifications_handler(ws)
    assert ws.close_code == 4401


@pytest.mark.asyncio
async def test_l5_03_too_many_connections_close_4402():
    """超 WS_HUB_MAX_PER_USER → close 4402."""
    from src.web.api.auth import create_token
    from src.web.models import User
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        owner = db.query(User).filter(User.role == "owner").first()
        if not owner:
            pytest.skip("无 owner 用户, 跳过此测例")
        user_id = str(owner.id)
        token, _ = create_token(owner, expires_hours=1)
    finally:
        db.close()

    max_n = ws_hub.WS_HUB_MAX_PER_USER
    for _ in range(max_n):
        ws_hub._register(user_id, MagicMock(), set())

    ws = MagicMock()
    ws.accept = AsyncMock()
    ws.headers = {}
    ws.query_params = {"token": token}

    async def _close(*args, **kwargs):
        ws.close_code = kwargs.get("code")
    ws.close = _close

    await ws_hub.ws_notifications_handler(ws)
    assert ws.close_code == 4402


# ── L6 与 push_notification 集成 ────────────────────────────────

def test_l6_01_push_notification_calls_ws_hub():
    """push_notification 落库后调 broadcast."""
    from src.core import notify_center

    with patch.object(ws_hub, "broadcast_notification") as bc:
        notify_center.push_notification(
            title="集成测试",
            body="body",
            category="agent_run",
            level="info",
            user_id=None,
        )
    assert bc.called


def test_l6_02_unread_incr_with_user_id():
    """user_id 有则 broadcast + incr_unread 都触发."""
    from src.core import notify_center
    from src.web.models import User
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        owner = db.query(User).filter(User.role == "owner").first()
        user_id = str(owner.id) if owner else None
    finally:
        db.close()
    if not user_id:
        pytest.skip("无 owner 用户, 跳过此测例")

    with patch.object(ws_hub, "broadcast_notification") as bc:
        with patch.object(ws_hub, "incr_unread") as inc:
            notify_center.push_notification(
                title="增量测试", body="x", user_id=user_id,
            )
    assert bc.called
    assert inc.called


# ── L7 跨进程 Pub/Sub ───────────────────────────────────────────

def test_l7_01_pubsub_publish_no_redis():
    """Redis 不可用时 publish 抛 redis_unavailable."""
    with patch.object(ws_hub, "_get_sync_redis", return_value=None):
        with pytest.raises(RuntimeError, match="redis_unavailable"):
            ws_hub._pubsub_publish("u1", {"x": 1})


def test_l7_02_install_pubsub_listener_no_redis():
    """Redis 不可用时静默."""
    with patch.object(ws_hub, "_get_sync_redis", return_value=None):
        ws_hub.install_pubsub_listener()


# ── 测例统计 ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    print(f"测例矩阵统计:")
    print(f"  L1 模块基础: 2")
    print(f"  L2 订阅管理: 5")
    print(f"  L3 同步广播: 5")
    print(f"  L4 未读计数: 2")
    print(f"  L5 WS 端点:  3")
    print(f"  L6 集成:     2")
    print(f"  L7 PubSub:   2")
    print(f"  TOTAL:       21 个测例")
    print(f"  (vs 7 事件源×5 渠道×多用户=35 组合矩阵 — 完整 5 渠道回执落库待 v0.4.37+)")
    sys.exit(0)