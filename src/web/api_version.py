"""API 版本化别名 (2026-08-21)。

提供 ApiVersionAliasMiddleware: 把 /api/v1/xxx 透明重写到 /api/xxx。

设计:
- 现有 289 个端点保持无 /v1 前缀(前端零改动);
- 新增 /api/v1/* 别名 → 将来引入破坏性 v2 时, 只需把别名中间件指向新路由表,
  老客户端继续用 /api/*, 新客户端迁 /api/v2/*, 平滑过渡;
- 中间件只改 request.url.path, 不产生重定向(30x 会改变浏览器行为且多一跳)。
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class ApiVersionAliasMiddleware(BaseHTTPMiddleware):
    """把 /api/v1/<rest> 内部改写为 /api/<rest> 后放行。"""

    def __init__(self, app, prefix: str = "/api/v1"):
        super().__init__(app)
        self._prefix = prefix.rstrip("/")

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path == self._prefix or path.startswith(self._prefix + "/"):
            rest = path[len(self._prefix):] or ""
            new_path = f"/api{rest}"
            # scope 级改写(BaseHTTPMiddleware 下改 request.url 不可靠, 直接动 scope)
            scope = request.scope
            scope["path"] = new_path
            scope["raw_path"] = new_path.encode()
            # query string 保持不变(scope["query_string"] 未动)
        return await call_next(request)
