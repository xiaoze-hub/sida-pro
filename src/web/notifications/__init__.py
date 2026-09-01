"""通知模块 (v0.4.36+):
- ws_hub: WS Hub (多用户 + 心跳 + Redis Pub/Sub 跨进程 + 未读计数)
- events: 7 类事件 schema + publish hook (后续 v0.4.37+ 接入)
"""
from src.web.notifications.ws_hub import (
    attach_event_loop,
    broadcast_notification,
    broadcast_global,
    incr_unread,
    get_unread,
    reset_unread,
    install_pubsub_listener,
    ws_notifications_handler,
    stats as ws_hub_stats,
)

__all__ = [
    "attach_event_loop",
    "broadcast_notification",
    "broadcast_global",
    "incr_unread",
    "get_unread",
    "reset_unread",
    "install_pubsub_listener",
    "ws_notifications_handler",
    "ws_hub_stats",
]