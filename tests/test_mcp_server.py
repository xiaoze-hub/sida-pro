"""MCP server（scripts/sida_mcp_server.py）契约回归。

钉四件事：
  1. MCP 握手/工具发现是标准形状（客户端能连上）；
  2. 工具层失败以 `result.isError` 返回，**不**把 server 打崩（一条坏请求不该中断会话）；
  3. **只读边界**：交易类名字一律拒绝，且目录里也不出现；
  4. 口径原样透传 —— MCP 出口不得把 caliber 吞掉或聚合。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.sida_mcp_server import handle, is_forbidden, make_handlers  # noqa: E402


def _transport_factory(calls: list[tuple[str, str, dict]]):
    def transport(method: str, url: str, **kw):
        calls.append((method, url, kw.get("body") or {}))
        if url.endswith("/api/skills/catalog"):
            return {
                "success": True,
                "data": {
                    "skills": [
                        {"name": "quote", "description": "实时行情", "caliber": "多源实时行情聚合",
                         "params": {"symbol": {"type": "string"}}, "required": ["symbol"]},
                        {"name": "chip", "description": "筹码", "caliber": "tick 逐笔"},
                        {"name": "place_order", "description": "不该出现", "caliber": "x"},
                    ]
                },
            }
        if "/api/skills/" in url and url.endswith("/run"):
            name = url.split("/api/skills/")[1].split("/run")[0]
            return {"success": True, "data": {"skill": name, "text": "600111 12.34 +2.1%",
                                              "caliber": "多源实时行情聚合"}}
        raise AssertionError(f"意外请求 {method} {url}")

    return transport


@pytest.fixture()
def env():
    calls: list[tuple[str, str, dict]] = []
    handlers = make_handlers(
        api_key="sk_test", base_url="https://example.test", transport=_transport_factory(calls)
    )
    return handlers, calls


def test_initialize_returns_protocol_and_tools_capability(env):
    handlers, _ = env
    out = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, handlers)
    res = out["result"]
    assert res["protocolVersion"]
    assert "tools" in res["capabilities"]
    assert res["serverInfo"]["name"] == "sida-data"
    assert res["serverInfo"]["version"]


def test_tools_list_exposes_two_meta_tools(env):
    handlers, _ = env
    out = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, handlers)
    names = [t["name"] for t in out["result"]["tools"]]
    assert names == ["sida_list_skills", "sida_run_skill"]
    # 每个工具都必须有 inputSchema，否则客户端无法构造调用
    assert all("inputSchema" in t for t in out["result"]["tools"])


def test_list_skills_filters_trading_and_keeps_caliber(env):
    handlers, _ = env
    out = handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "sida_list_skills", "arguments": {}}},
        handlers,
    )
    payload = json.loads(out["result"]["content"][0]["text"])
    names = [s["name"] for s in payload["skills"]]
    assert "quote" in names and "chip" in names
    assert "place_order" not in names          # 交易类既不可调也不可见
    assert payload["count"] == 2
    quote = next(s for s in payload["skills"] if s["name"] == "quote")
    assert quote["caliber"] == "多源实时行情聚合"   # 口径原样透传


def test_run_skill_posts_params_and_returns_text(env):
    handlers, calls = env
    out = handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "sida_run_skill",
                    "arguments": {"name": "quote", "params": {"symbol": "600111"}}}},
        handlers,
    )
    assert "isError" not in out["result"]
    text = out["result"]["content"][0]["text"]
    assert "600111" in text and "多源实时行情聚合" in text
    assert calls[-1][0] == "POST"
    assert calls[-1][1].endswith("/api/skills/quote/run")
    assert calls[-1][2] == {"params": {"symbol": "600111"}}


def test_run_skill_rejects_trading_names(env):
    handlers, calls = env
    out = handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "sida_run_skill", "arguments": {"name": "place_order"}}},
        handlers,
    )
    assert out["result"]["isError"] is True
    assert "只读" in out["result"]["content"][0]["text"]
    assert not any("/run" in c[1] for c in calls)  # 根本没发出请求


def test_gateway_failure_becomes_isError_not_crash():
    def transport(method, url, **kw):
        raise __import__("scripts.sida_mcp_server", fromlist=["ToolError"]).ToolError("网关返回 HTTP 429: 配额用尽")

    handlers = make_handlers(api_key="sk_t", base_url="https://x.test", transport=transport)
    out = handle(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "sida_run_skill", "arguments": {"name": "quote"}}},
        handlers,
    )
    assert out["result"]["isError"] is True
    assert "429" in out["result"]["content"][0]["text"]


def test_unknown_method_and_tool_are_errors(env):
    handlers, _ = env
    assert handle({"jsonrpc": "2.0", "id": 7, "method": "nope"}, handlers)["error"]["code"] == -32601
    bad = handle({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                  "params": {"name": "nope", "arguments": {}}}, handlers)
    assert bad["error"]["code"] == -32602


def test_missing_name_is_tool_error(env):
    handlers, _ = env
    out = handle(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
         "params": {"name": "sida_run_skill", "arguments": {}}},
        handlers,
    )
    assert out["result"]["isError"] is True


def test_notification_returns_none(env):
    handlers, _ = env
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, handlers) is None


def test_is_forbidden_matrix():
    for bad in ["place_order", "trade_exec", "buy_stock", "cancel_order", "SELL_ALL"]:
        assert is_forbidden(bad) is True
    for ok in ["quote", "chip_analyst", "main_intent", "kline"]:
        assert is_forbidden(ok) is False
