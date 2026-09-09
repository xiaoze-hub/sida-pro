"""FastAPI 应用装配(W3.2/D1): agent 自动发现 → lifespan 挂载 → SPA 兜底。

路由/中间件本体仍在 src/web/app.py(模块级单例); 本模块负责把启动行为
(bootstrap lifespan 与静态 SPA 服务)装配上去并保持幂等。server.py 只留
`app = create_app()`。
"""

from __future__ import annotations

import logging
import os

from src.bootstrap.env import REPO_ROOT

logger = logging.getLogger("server")

_bootstrapped = False


def _static_dir() -> str:
    return str(REPO_ROOT / "static")


def _mount_spa(app) -> None:
    """生产环境静态文件服务(原 server.py 模块级实现搬移)。

    SPA 路由：非 API 请求返回 index.html；但静态资源(.js/.css/图片等)
    不存在时必须返回 404,不能回退 index.html —— 否则浏览器把 HTML 当 JS 执行,
    直接白屏(旧 index.html 引用旧 hash 资源时必现)。
    """
    static_dir = _static_dir()
    if not os.path.exists(static_dir):
        return
    from fastapi.responses import FileResponse

    _SPA_ASSET_EXT = (
        ".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
        ".ico", ".woff", ".woff2", ".ttf", ".map", ".json",
    )

    @app.get("/{path:path}")
    async def serve_spa(path: str):
        # /api/* 未注册的端点 → 返 404 JSON 而非 SPA HTML(避免前端 JSON.parse(HTML) 触发 ErrorBoundary)
        if path.startswith("api/"):
            from fastapi.responses import JSONResponse

            return JSONResponse(
                status_code=404,
                content={
                    "code": 404,
                    "success": False,
                    "data": None,
                    "message": f"API endpoint not found: /{path}",
                },
            )
        file_path = os.path.join(static_dir, path)
        # P0 (2026-09-05 28号审计): 路径穿越校验, 越界一律 404(未鉴权路由)
        real = os.path.realpath(file_path)
        if real != os.path.realpath(static_dir) and not real.startswith(
            os.path.realpath(static_dir) + os.sep
        ):
            from fastapi.responses import Response as _Resp

            return _Resp(status_code=404)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        # 资源类路径不存在 → 404(绝不回退 index.html)
        if path.startswith("assets/") or path.lower().endswith(_SPA_ASSET_EXT):
            from fastapi.responses import Response

            return Response(status_code=404)
        return FileResponse(os.path.join(static_dir, "index.html"))

    logger.info(f"静态文件服务已启用: {static_dir}")


def create_app():
    """装配 FastAPI 单例并挂载启动行为。幂等: 重复调用不重复挂路由。"""
    global _bootstrapped

    from src.bootstrap.agents import discover_agents

    discover_agents()

    from src.bootstrap.startup import lifespan
    from src.web.app import app

    if not _bootstrapped:
        app.router.lifespan_context = lifespan
        _mount_spa(app)
        _bootstrapped = True
    return app
