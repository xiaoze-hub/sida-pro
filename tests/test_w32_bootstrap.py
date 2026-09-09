"""W3.2/D1: server.py 拆分 src/bootstrap/ 后的行为保持验证。

验收口径(方案 3.2):
- server.py ≤ 50 行(纯 shim: create_app 装配 + PEP 562 兼容转发 + uvicorn 入口)。
- 新增 agent 只需一个文件 + 一个装饰器(demo agent 写入 src/agents/ 自动发现, 验后删)。
- 存量 `from server import X` / `import server; server.scheduler` 兼容面不破:
  调度器全局是"运行期被 lifespan 写入的可变状态", shim 必须转发 live 值,
  不允许 import 期按值拷贝(否则 health.py 深检永远读到 None)。
- create_app() 装配: lifespan 挂载 + SPA 兜底路由 + 幂等。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PY = REPO_ROOT / "server.py"


def test_server_py_is_thin_shim_le_50_lines():
    lines = SERVER_PY.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 50, f"server.py 应 ≤50 行(shim), 实际 {len(lines)} 行"
    body = "\n".join(lines)
    assert "create_app" in body
    assert "AGENT_REGISTRY" not in body, "注册表必须搬进 src/bootstrap/agents.py"
    assert "def lifespan" not in body, "lifespan 必须搬进 src/bootstrap/startup.py"


def test_shim_forwards_live_scheduler_state():
    """health.py 深检语义: import server 后读 scheduler 必须转发 runtime 的 live 值。"""
    import server  # noqa: F401
    from src.bootstrap import runtime as rt

    sentinel = object()
    try:
        rt.price_alert_scheduler = sentinel
        assert server.price_alert_scheduler is sentinel, (
            "server.<attr> 必须转发 src.bootstrap.runtime 的运行期值(PEP 562), "
            "按值 re-export 会在 lifespan 写入前固化 None"
        )
    finally:
        rt.price_alert_scheduler = None
    assert server.price_alert_scheduler is None


def test_shim_from_import_surface_unchanged():
    """存量调用方逐一对账(2026-09-09 grep 全仓所得, 缺一个都是破兼容)。"""
    from server import (  # noqa: F401
        _kline_oneoff_loop,
        _seed_providers_by_type,
        apply_proxy_env,
        build_context,
        load_portfolio_for_agent,
        load_watchlist_for_agent,
        price_alert_scheduler,
        reconcile_data_sources,
        reload_scheduler,
        trigger_agent,
        trigger_agent_for_stock,
    )

    import server as srv  # noqa: F401

    for attr in (
        "scheduler",
        "paper_trading_scheduler",
        "context_maintenance_scheduler",
        "kline_backfill_scheduler",
    ):
        assert hasattr(srv, attr), f"server.{attr} 深检/调用方需要"


def test_builtin_agents_registered_by_decorator():
    """9 个内置 agent 经装饰器注册(不再有 server.py 手工 dict)。"""
    from src.bootstrap.agents import AGENT_REGISTRY
    from src.bootstrap.app import create_app

    create_app()  # 触发 discover_agents()

    expected = {
        "daily_report",
        "premarket_outlook",
        "news_digest",
        "chart_analyst",
        "intraday_monitor",
        "tradingagents",
        "theme_launch_detector",
        "stock_attribution",
        "auction_review",
    }
    missing = expected - set(AGENT_REGISTRY)
    assert not missing, f"内置 agent 未被装饰器自动发现: {missing}"
    assert AGENT_REGISTRY["daily_report"].__name__ == "DailyReportAgent"


def test_new_agent_needs_only_file_and_decorator(tmp_path):
    """验收: 新增一个 agent 只需一个文件 + 一个装饰器(demo 写入 src/agents/ 验后删)。"""
    from src.bootstrap.agents import AGENT_REGISTRY, discover_agents

    demo = REPO_ROOT / "src" / "agents" / "w32_demo_agent.py"
    demo.write_text(
        '"""W3.2 demo agent(测试用, 即写即删)。"""\n'
        "from src.bootstrap.agents import register_agent\n"
        "\n"
        "\n"
        '@register_agent("w32_demo")\n'
        "class W32DemoAgent:\n"
        "    pass\n",
        encoding="utf-8",
    )
    sys.modules.pop("src.agents.w32_demo_agent", None)
    try:
        assert "w32_demo" not in AGENT_REGISTRY
        discover_agents()
        assert AGENT_REGISTRY["w32_demo"].__name__ == "W32DemoAgent"
        # 幂等: 重复发现不重复注册/不报错
        before = dict(AGENT_REGISTRY)
        discover_agents()
        assert AGENT_REGISTRY == before
    finally:
        demo.unlink(missing_ok=True)
        sys.modules.pop("src.agents.w32_demo_agent", None)
        AGENT_REGISTRY.pop("w32_demo", None)
    assert "w32_demo" not in AGENT_REGISTRY


def test_create_app_assembles_and_is_idempotent():
    from src.bootstrap.app import create_app
    from src.web.app import app as web_app

    app1 = create_app()
    assert app1 is web_app, "create_app 应装配既有 FastAPI 单例(路由在 src/web/app.py)"
    # lifespan 已挂载(不再是 FastAPI 默认的 _wrap_asgi/空 lifespan)
    assert app1.router.lifespan_context is not None
    n_routes = len(app1.routes)
    app2 = create_app()
    assert app2 is app1
    assert len(app2.routes) == n_routes, "create_app 必须幂等, 不得重复挂 SPA 路由"


def test_spa_and_ssl_paths_resolve_to_repo_root():
    """搬进 src/bootstrap/ 后, static/data 相对路径必须仍解析到仓库根
    (容器内 = /app; 搬家后 __file__ 变深两级, 算错会白屏/丢 CA)。"""
    from src.bootstrap.app import _static_dir
    from src.bootstrap.env import _ca_bundle_path

    assert _static_dir() == str(REPO_ROOT / "static")
    assert _ca_bundle_path() == str(REPO_ROOT / "data" / "ca-bundle.pem")
