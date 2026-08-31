"""主力意图接入交易智能体的单元测试(2026-08-22)。

覆盖:
1. build_main_intent_context 纯函数: 正常渲染 / None 返回空 / 空串返回空
2. collect() 返回 dict 含 main_intent 字段(A 股采集 / 非 A 股为 None / 失败降级)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.tradingagents.portfolio_context import build_main_intent_context


# ============================================================
# 1. build_main_intent_context 纯函数
# ============================================================

def test_main_intent_context_renders_block():
    text = "主力净流入+尾盘加仓(吸筹) | 参与度88%买占49% | 筹码峰11.41 获利74%"
    out = build_main_intent_context(text)
    assert "[Main Force Intent" in out
    assert "主力净流入+尾盘加仓(吸筹)" in out
    assert "腾讯逐笔口径" in out


def test_main_intent_context_none_returns_empty():
    assert build_main_intent_context(None) == ""
    assert build_main_intent_context("") == ""
    assert build_main_intent_context("   ") == ""


# ============================================================
# 2. collect() 返回含 main_intent 字段
# ============================================================

class _StockMt:
    name = "贵州茅台"
    symbol = "600519"
    market = type("M", (), {"value": "CN"})()


class _StockHk:
    name = "腾讯控股"
    symbol = "00700"
    market = type("M", (), {"value": "HK"})()


def _make_agent():
    from src.agents.tradingagents.agent import TradingAgentsAgent

    agent = TradingAgentsAgent.__new__(TradingAgentsAgent)
    agent._available = True
    agent._import_error = ""
    agent.name = "tradingagents"
    agent.monthly_budget_usd = 100.0
    agent.over_budget_action = "warn"
    return agent


class _Ctx:
    def __init__(self, stock):
        self.watchlist = [stock]


def test_collect_cn_includes_main_intent_field():
    """A 股 collect 返回 dict 含 main_intent 字段(值来自 _main_intent_summary)。"""
    agent = _make_agent()
    ctx = _Ctx(_StockMt())

    fake_summary = "主力净流入+尾盘加仓(吸筹) | 参与度88%买占49%"

    with patch(
        "src.agents.intraday_monitor._main_intent_summary",
        return_value=fake_summary,
    ), patch(
        "src.agents.tradingagents.agent.get_market_data",
        return_value=MagicMock(),
    ), patch(
        "src.agents.tradingagents.agent._fetch_ta_capital_flow",
        return_value=None,
    ), patch(
        "src.agents.tradingagents.agent.asyncio.gather",
        new=AsyncMock(return_value=([], [], None, [])),
    ), patch(
        "src.collectors.kline_collector.KlineCollector",
        return_value=MagicMock(),
    ), patch(
        "src.agents.tradingagents.agent._collect_a_share_sentiment",
        return_value=None,
    ), patch(
        "src.agents.tradingagents.financial_data.fetch_financial_abstract",
        return_value=None,
    ):
        import asyncio

        data = asyncio.run(agent.collect(ctx))

    assert "main_intent" in data
    assert data["main_intent"] == fake_summary


def test_collect_main_intent_failure_degrades_to_none():
    """主力意图采集抛异常时降级为 None, 不阻塞 collect 返回。"""
    agent = _make_agent()
    ctx = _Ctx(_StockMt())

    def _boom(*a, **k):
        raise RuntimeError("network down")

    with patch(
        "src.agents.intraday_monitor._main_intent_summary",
        side_effect=_boom,
    ), patch(
        "src.agents.tradingagents.agent.get_market_data",
        return_value=MagicMock(),
    ), patch(
        "src.agents.tradingagents.agent._fetch_ta_capital_flow",
        return_value=None,
    ), patch(
        "src.agents.tradingagents.agent.asyncio.gather",
        new=AsyncMock(return_value=([], [], None, [])),
    ), patch(
        "src.collectors.kline_collector.KlineCollector",
        return_value=MagicMock(),
    ), patch(
        "src.agents.tradingagents.agent._collect_a_share_sentiment",
        return_value=None,
    ), patch(
        "src.agents.tradingagents.financial_data.fetch_financial_abstract",
        return_value=None,
    ):
        import asyncio

        data = asyncio.run(agent.collect(ctx))

    assert "main_intent" in data
    assert data["main_intent"] is None
