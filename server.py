"""PanWatch 统一服务入口 —— W3.2/D1 起为兼容 shim, 实现在 src/bootstrap/。

app 装配=bootstrap/app.py; agent 注册表=agents.py(@register_agent 自动发现);
数据源种子=datasources.py; 调度器/触发=runtime.py; 启动编排=startup.py;
代理/SSL/日志/Playwright=env.py。本文件仅留装配 + 兼容转发 + uvicorn 入口。
"""
import os

from src.bootstrap.app import create_app

app = create_app()

# 兼容转发: 调度器实例等是运行期被 lifespan 写入的可变状态, 必须经
# PEP 562 __getattr__ 读 live 值, 不能 import 期按值 re-export(否则固化 None)。
_FORWARD_MODULES = (
    "src.bootstrap.runtime",
    "src.bootstrap.datasources",
    "src.bootstrap.agents",
    "src.bootstrap.env",
)


def __getattr__(name: str):
    import importlib

    for _m in _FORWARD_MODULES:
        _mod = importlib.import_module(_m)
        if hasattr(_mod, name):
            return getattr(_mod, name)
    raise AttributeError(f"module 'server' has no attribute {name!r}")


if __name__ == "__main__":
    print("盯盘侠启动: http://127.0.0.1:8000")
    print("API 文档: 已关闭(生产安全策略, 防接口地图泄露)")
    # 生产(Docker)不开 reload; 本地热重载用 `make dev-api` 或 DEV_RELOAD=1。
    _dev_reload = os.environ.get("DEV_RELOAD", "").lower() in ("1", "true", "yes")
    _workers = int(os.environ.get("WEB_WORKERS", "2"))
    _host = os.environ.get("WEB_HOST", "127.0.0.1")  # 公网须显式 0.0.0.0 + 前置反代
    import uvicorn

    uvicorn.run(
        "server:app", host=_host, port=8000, workers=_workers, reload=_dev_reload,
        reload_dirs=["src"] if _dev_reload else None,
        reload_excludes=["data/*", "frontend/*", ".claude/*"] if _dev_reload else None,
    )
