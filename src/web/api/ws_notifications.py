"""通知 WebSocket 路由 (v0.4.36 P0 派活 1)。

⚠️ 不能挂 dependencies=protected(HTTPBearer): WS 握手会被 Bearer auth 抛 500。
   鉴权在 handler 内做 (复用 ws_quotes 的 Sec-WebSocket-Protocol / ?token= 模式)。

端点: ws://host/api/notifications/ws[?token=<jwt>]
"""
import logging

from fastapi import APIRouter, WebSocket

from src.web.notifications.ws_hub import ws_notifications_handler

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/notifications/ws")
async def notifications_ws(websocket: WebSocket):
    """实时通知推送 (登录用户)."""
    await ws_notifications_handler(websocket)