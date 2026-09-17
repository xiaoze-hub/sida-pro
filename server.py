"""PanWatch 统一服务入口 —— W3.2/D1 起为兼容 shim, 实现在 src/bootstrap/。

app 装配=bootstrap/app.py; agent 注册表=agents.py(@register_agent 自动发现);
数据源种子=datasources.py; 调度器/触发=runtime.py; 启动编排=startup.py;
代理/SSL/日志/Playwright=env.py; uvicorn 入口=cli.py。
本文件仅留装配 + 兼容转发 + 一行入口(≤50 行由 tests/test_w32_bootstrap.py 钉住)。
"""
from src.bootstrap.app import create_app
from src.bootstrap.cli import run as _run

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
    _run()
