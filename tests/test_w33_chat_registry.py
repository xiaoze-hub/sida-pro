# -*- coding: utf-8 -*-
"""W3.3 (D3) chat 工具注册表重构验收。

风险整改方案 3.3 验收项:
1. src/core 下死代码工具包装模块已删除; 全仓 *.py 内容 grep 命中 0。
2. 注册表: schema 与 handler 同处注册, 每个工具含 caliber(口径标签, 非空)
   + requires(权限点); get_capital_flow caliber 为固定红线文案。
3. chat_api.CHAT_TOOLS 由注册表导出: 29 个核心 schema(原顺序) + 11 个 thsdk。
4. 合并后 _run_tool_loop(stream=True/False) 与原两函数事件语义等价(mock AI client):
   正常出字 / tool 轮次 / chat_multi 回落 / LLMDegradedError 不回落 /
   MAX_TOOL_ROUNDS 兜底 / 异常兜底。
5. 流式端点含断开检测(结构性检查; 真实断开行为用 curl Ctrl-C 手工验证, 见 CHANGELOG)。
"""

import asyncio
import dataclasses
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_NEEDLE = "chat_" + "tools"  # 拼接构造, 避免本文件自匹配

EXPECTED_CORE_ORDER = [
    "get_portfolio", "get_stock_quote", "get_technical_analysis", "get_main_intent",
    "get_decision_pioneer", "get_rally_analysis", "get_stock_suggestions", "get_watchlist",
    "get_capital_flow", "tdx_wenda", "get_market_news", "get_kline_patterns",
    "get_auction_data", "get_forecast", "get_sentiment_cycle", "get_strategy_signals",
    "get_notifications", "get_fundamentals_detail", "get_irm_qa", "get_market_anomalies",
    "get_northbound", "get_hot_stocks", "get_web_content", "get_main_flow_compare",
    "get_delta_series", "get_orderbook", "get_event_catalyst", "get_intent_explain",
    "get_factor_ic_report",
]

EXPECTED_THSDK = {
    "get_thsdk_news", "get_thsdk_corporate_action", "get_thsdk_dde",
    "get_thsdk_hs300_constituents", "get_thsdk_market_data_cn_extended",
    "get_thsdk_market_data_index", "get_thsdk_market_data_hk",
    "get_thsdk_market_data_us", "get_thsdk_market_data_bond",
    "get_thsdk_market_data_fund", "get_wencai_enhanced",
}


# ──────────────────────────── 1. 死代码删除 ────────────────────────────

def test_dead_wrapper_module_deleted():
    assert not (ROOT / "src" / "core" / (_NEEDLE + ".py")).exists()


def test_no_residual_wrapper_references():
    hits = []
    for p in ROOT.rglob("*.py"):
        s = set(p.parts)
        if "__pycache__" in s or ".git" in s or p.name == Path(__file__).name:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _NEEDLE in text:
            hits.append(str(p.relative_to(ROOT)))
    assert hits == [], f"死代码包装模块仍有引用: {hits}"


# ──────────────────────────── 2. 注册表完整性 ────────────────────────────

def test_registry_integrity():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY
    assert set(CHAT_TOOL_REGISTRY) == (
        set(EXPECTED_CORE_ORDER) | EXPECTED_THSDK | {"get_opportunities"}
    )
    assert len(CHAT_TOOL_REGISTRY) == 41  # 29 core + 1 handler-only + 11 thsdk
    for name, tool in CHAT_TOOL_REGISTRY.items():
        assert tool.caliber and tool.caliber.strip(), f"{name} 缺口径标签 caliber"
        assert callable(tool.handler), f"{name} handler 不可调用"
        assert tool.requires in ("", "self"), f"{name} requires 非法: {tool.requires}"
        if tool.schema is not None:
            assert tool.schema["function"]["name"] == name, f"{name} schema 名与注册名不一致"


def test_get_capital_flow_caliber_redline():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY
    assert (
        CHAT_TOOL_REGISTRY["get_capital_flow"].caliber
        == "东财四档，方向位与逐笔口径相反，禁止据此判定主力意图"
    )


def test_self_requires_scoping():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY
    self_tools = {n for n, t in CHAT_TOOL_REGISTRY.items() if t.requires == "self"}
    assert self_tools == {
        "get_portfolio", "get_stock_suggestions", "get_watchlist", "get_notifications",
    }


def test_handler_only_tool_has_no_schema():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY
    assert CHAT_TOOL_REGISTRY["get_opportunities"].schema is None


def test_duplicate_registration_rejected():
    from src.agents.chat import registry as reg

    with pytest.raises(ValueError):
        @reg.register_chat_tool("get_watchlist", caliber="x")
        async def _dup(db, args, user):
            return ""


# ──────────────────────────── 3. CHAT_TOOLS 导出面 ────────────────────────────

def test_exported_tool_order():
    from src.web.api import chat as chat_api
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY
    names = [t["function"]["name"] for t in chat_api.CHAT_TOOLS]
    assert names[: len(EXPECTED_CORE_ORDER)] == EXPECTED_CORE_ORDER
    assert set(names[len(EXPECTED_CORE_ORDER):]) == EXPECTED_THSDK
    with_schema = {n for n, t in CHAT_TOOL_REGISTRY.items() if t.schema is not None}
    assert set(names) == with_schema


def test_execute_tool_dispatcher(monkeypatch):
    from src.web.api import chat as chat_api
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    assert asyncio.run(chat_api._execute_tool(None, "no_such_tool", {})) == "未知工具: no_such_tool"

    async def boom(db, args, user):
        raise ValueError("boom")

    tool = CHAT_TOOL_REGISTRY["get_watchlist"]
    monkeypatch.setitem(CHAT_TOOL_REGISTRY, "get_watchlist", dataclasses.replace(tool, handler=boom))
    out = asyncio.run(chat_api._execute_tool(None, "get_watchlist", {}))
    assert out == "工具执行出错: boom"


# ──────────────────────────── 4. 合并后 tool loop 语义 ────────────────────────────

class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _ToolCall:
    def __init__(self, cid="t1", name="get_portfolio", arguments="{}"):
        self.id = cid

        class _F:
            pass

        _F.name = name
        _F.arguments = arguments
        self.function = _F


class _Client:
    """非流式 mock: chat_with_tools 逐轮弹出; 异常对象抛出。"""

    def __init__(self, responses, multi="multi-兜底回复"):
        self._responses = list(responses)
        self._multi = multi
        self.chat_multi_calls = 0

    async def chat_with_tools(self, messages, tools=None, temperature=0.5):
        if not self._responses:
            raise RuntimeError("responses exhausted(第 6 轮不应再调 LLM)")
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def chat_multi(self, messages, temperature=0.5):
        self.chat_multi_calls += 1
        return self._multi


class _StreamClient:
    """流式 mock: 每轮是一组 (kind, payload) 事件或 _Msg(final) 或异常。"""

    def __init__(self, rounds, multi="multi-兜底回复"):
        self._rounds = list(rounds)
        self._multi = multi
        self.chat_multi_calls = 0
        self._idx = 0

    def chat_with_tools_stream(self, messages, tools=None, temperature=0.5):
        return self._gen()

    async def _gen(self):
        # 跨调用累计推进: 每次 chat_with_tools_stream 消费一轮, 不重置
        item = self._rounds[self._idx] if self._idx < len(self._rounds) else None
        self._idx += 1
        if item is None:
            raise RuntimeError("rounds exhausted(第 6 轮不应再调 LLM)")
        if isinstance(item, Exception):
            raise item
        if isinstance(item, _Msg):
            yield "final", item
            return
        for ev in item:
            yield ev

    async def chat_multi(self, messages, temperature=0.5):
        self.chat_multi_calls += 1
        return self._multi


async def _collect(agen):
    out = []
    async for ev in agen:
        out.append(ev)
    return out


def test_nonstream_plain_text():
    from src.web.api import chat as chat_api
    client = _Client([_Msg(content="你好")])
    evs = asyncio.run(_collect(chat_api._run_tool_loop(client, [], db=None)))
    assert evs == [("text", "你好")]


def test_nonstream_tool_round_then_text(monkeypatch):
    from src.web.api import chat as chat_api
    calls = []

    async def fake_exec(db, name, args, user=None):
        calls.append((name, args))
        return "持仓: 1 只"

    monkeypatch.setattr(chat_api, "_execute_tool", fake_exec)
    client = _Client([
        _Msg(content=None, tool_calls=[_ToolCall("t1", "get_portfolio", "{}")]),
        _Msg(content="您的持仓良好"),
    ])
    msgs: list[dict] = []
    evs = asyncio.run(_collect(chat_api._run_tool_loop(client, msgs, db=None)))
    assert [k for k, _ in evs] == ["stage", "text"]
    assert evs[0][1] == "正在读取您的持仓..."
    assert evs[1] == ("text", "您的持仓良好")
    assert calls == [("get_portfolio", {})]
    # assistant(tool_calls) + tool(result) 两条消息入列
    assert [m["role"] for m in msgs] == ["assistant", "tool"]
    assert msgs[1]["tool_call_id"] == "t1"


def test_nonstream_fallback_chat_multi():
    from src.web.api import chat as chat_api
    client = _Client([RuntimeError("模型不支持 tool use")])
    evs = asyncio.run(_collect(chat_api._run_tool_loop(client, [], db=None)))
    assert evs == [("text", "multi-兜底回复")]
    assert client.chat_multi_calls == 1


def test_nonstream_llm_degraded_no_retry():
    """0.3: 降级≠工具不支持, 不许走 chat_multi 兜底重试放大; 外层产出明确文案。"""
    from src.core.ai_client import LLMDegradedError
    from src.web.api import chat as chat_api
    client = _Client([LLMDegradedError("服务降级", scene="chat")])
    evs = asyncio.run(_collect(chat_api._run_tool_loop(client, [], db=None)))
    assert evs == [("text", "抱歉，AI 服务暂时不可用：LLM 降级(chat): 服务降级")]
    assert client.chat_multi_calls == 0


def test_nonstream_rounds_exhausted(monkeypatch):
    from src.web.api import chat as chat_api

    async def fake_exec(db, name, args, user=None):
        return "x"

    monkeypatch.setattr(chat_api, "_execute_tool", fake_exec)
    client = _Client([_Msg(content=None, tool_calls=[_ToolCall(f"t{i}")]) for i in range(5)])
    evs = asyncio.run(_collect(chat_api._run_tool_loop(client, [], db=None)))
    # 每轮 tool 前先产 stage 事件, 5 轮后 MAX_TOOL_ROUNDS 兜底
    assert evs == [("stage", "正在读取您的持仓...")] * 5 + [
        ("text", "抱歉，处理轮次过多，请精简问题再试。")
    ]


def test_nonstream_exception_bottomline(monkeypatch):
    from src.web.api import chat as chat_api

    async def fake_exec(db, name, args, user=None):
        raise RuntimeError("工具炸了(逃过分支内兜底)")

    monkeypatch.setattr(chat_api, "_execute_tool", fake_exec)
    client = _Client([
        _Msg(content=None, tool_calls=[_ToolCall("t1", "get_portfolio")]),
        _Msg(content="不应到达"),
    ])
    evs = asyncio.run(_collect(chat_api._run_tool_loop(client, [], db=None)))
    # stage 先产出, 随后异常穿透到外层兜底
    assert len(evs) == 2 and evs[0][0] == "stage" and evs[1][0] == "text"
    assert evs[1][1].startswith("抱歉，AI 服务暂时不可用：")


def test_stream_delta_and_done():
    from src.web.api import chat as chat_api
    client = _StreamClient([[("delta", "你"), ("delta", "好"), ("final", _Msg(content="你好"))]])
    evs = asyncio.run(_collect(chat_api._run_tool_loop(client, [], db=None, stream=True)))
    assert evs == [("delta", "你"), ("delta", "好"), ("done", "你好")]


def test_stream_tool_round_then_deltas(monkeypatch):
    from src.web.api import chat as chat_api

    async def fake_exec(db, name, args, user=None):
        return "持仓: 1 只"

    monkeypatch.setattr(chat_api, "_execute_tool", fake_exec)
    client = _StreamClient([
        [("final", _Msg(content=None, tool_calls=[_ToolCall("t1", "get_portfolio")]))],
        [("delta", "结果"), ("final", _Msg(content=None))],
    ])
    evs = asyncio.run(_collect(chat_api._run_tool_loop(client, [], db=None, stream=True)))
    assert evs == [
        ("stage", "正在读取您的持仓..."),
        ("delta", "结果"),
        ("done", "结果"),
    ]


def test_stream_fallback_chat_multi():
    from src.web.api import chat as chat_api
    client = _StreamClient([RuntimeError("流式 tool 不支持")])
    evs = asyncio.run(_collect(chat_api._run_tool_loop(client, [], db=None, stream=True)))
    assert evs == [("delta", "multi-兜底回复"), ("done", "multi-兜底回复")]
    assert client.chat_multi_calls == 1


def test_stream_llm_degraded_no_retry():
    from src.core.ai_client import LLMDegradedError
    from src.web.api import chat as chat_api
    client = _StreamClient([LLMDegradedError("服务降级", scene="chat")])
    evs = asyncio.run(_collect(chat_api._run_tool_loop(client, [], db=None, stream=True)))
    assert evs == [("done", "抱歉，AI 服务暂时不可用：LLM 降级(chat): 服务降级")]
    assert client.chat_multi_calls == 0


def test_stream_rounds_exhausted(monkeypatch):
    from src.web.api import chat as chat_api

    async def fake_exec(db, name, args, user=None):
        return "x"

    monkeypatch.setattr(chat_api, "_execute_tool", fake_exec)
    client = _StreamClient([
        [("final", _Msg(content=None, tool_calls=[_ToolCall(f"t{i}")]))] for i in range(5)
    ])
    evs = asyncio.run(_collect(chat_api._run_tool_loop(client, [], db=None, stream=True)))
    assert evs == [("stage", "正在读取您的持仓...")] * 5 + [
        ("done", "抱歉，处理轮次过多，请精简问题再试。")
    ]


# ──────────────────────────── 5. 断开检测 + 行数 ────────────────────────────

def test_stream_endpoint_has_disconnect_guard():
    from src.web.api import chat as chat_api
    src = inspect.getsource(chat_api.send_message_stream)
    assert "is_disconnected" in src, "流式端点缺客户端断开检测"
    assert "CancelledError" in src, "流式端点缺取消处理"


def test_chat_py_line_shrink():
    lines = (ROOT / "src" / "web" / "api" / "chat.py").read_text(encoding="utf-8").count("\n")
    assert lines <= 2221, f"chat.py 应较 3021 行下降 ≥800, 当前 {lines}"


def test_exec_thsdk_tool_compat_surface():
    """既有测试面: 未知 thsdk 工具降级文案保持原样。"""
    from src.web.api import chat as chat_api
    out = asyncio.run(chat_api._exec_thsdk_tool("get_thsdk_not_real", {}))
    assert out == "[thsdk] 工具 get_thsdk_not_real 尚未注册实现。"
