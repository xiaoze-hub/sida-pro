"""`python server.py` 的启动逻辑(W3.2 shim 瘦身: 从 server.py 搬出, 2026-09-18)。

为什么单独一个模块: `tests/test_w32_bootstrap.py` 钉着「server.py ≤ 50 行纯 shim」——
本文件承接"跑起来"的那部分(日志/worker 数/uvicorn.run), 让 server.py 只剩
`create_app()` + PEP 562 兼容转发。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("server")


def run() -> None:
    """uvicorn 入口(`python server.py` → 本函数)。"""
    print("盯盘侠启动: http://127.0.0.1:8000")
    print("API 文档: 已关闭(生产安全策略, 防接口地图泄露)")
    # 生产(Docker)不开 reload; 本地热重载用 `make dev-api` 或 DEV_RELOAD=1。
    _dev_reload = os.environ.get("DEV_RELOAD", "").lower() in ("1", "true", "yes")
    # 并发优化 2026-09-15(Skill Gateway 压测): 默认 4 worker。
    # 慢启动由 Dockerfile healthcheck start-period=90s 兜住; 内存紧张可 WEB_WORKERS=1。
    _workers = int(os.environ.get("WEB_WORKERS", "4"))
    if _workers > 1:
        logger.info(
            "WEB_WORKERS=%d: 多进程并行(Skill Gateway 并发)。若启动期 Child process died, "
            "改设 WEB_WORKERS=1 并检查 healthcheck start-period。",
            _workers,
        )
    _host = os.environ.get("WEB_HOST", "127.0.0.1")  # 公网须显式 0.0.0.0 + 前置反代
    import uvicorn

    uvicorn.run(
        "server:app",
        host=_host,
        port=8000,
        workers=_workers,
        reload=_dev_reload,
        reload_dirs=["src"] if _dev_reload else None,
        reload_excludes=["data/*", "frontend/*", ".claude/*"] if _dev_reload else None,
    )
