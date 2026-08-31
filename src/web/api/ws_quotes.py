"""行情 WebSocket 独立 router(2026-08-12)。

⚠️ 不能放 quotes.py: 该 router 挂了 dependencies=protected(HTTPBearer),
   WebSocket 握手走 HTTPBearer.__call__(request) 会 TypeError → 500。
   故独立成无 auth 依赖的 router, handler 内自行解析 query token 校验。
"""
import logging

from fastapi import APIRouter, WebSocket

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/quotes/ws")
async def quote_ws(websocket: WebSocket):
    """行情 WebSocket 推送: 自选股实时行情。

    前端连 ws://host/api/quotes/ws?token=<jwt>
    handler 内校验 token(路由级无 auth 依赖)。
    """
    from src.web.api.quote_stream import websocket_quote_handler

    await websocket_quote_handler(websocket)
