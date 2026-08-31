"""盘前分析接入事件驱动预期差引擎的单元测试(2026-08-22)。

覆盖:
1. build_prompt 渲染「个股事件催化与预期差」段(有 catalyst_analysis 数据)
2. build_prompt 在 catalyst_analysis 为空时不出该段(回归)
3. collect() 采集 catalyst_analysis: 并发调 analyze_event_catalyst, A股才调, 失败降级
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.premarket_outlook import PremarketOutlookAgent
from src.agents.base import AgentContext
from src.config import AppConfig, StockConfig
from src.models.market import MarketCode


def _make_context(watchlist) -> AgentContext:
    """构造最小 AgentContext(只测 build_prompt / collect 的 catalyst 路径)。"""
    from src.config import Settings

    config = AppConfig(settings=Settings(), watchlist=watchlist)
    return AgentContext(
        ai_client=MagicMock(),
        notifier=MagicMock(),
        config=config,
    )


def _cn_stock(symbol="002361", name="神剑股份"):
    return StockConfig(symbol=symbol, name=name, market=MarketCode.CN)


def _catalyst_result():
    return {
        "catalyst": "军工材料涨价",
        "direction": "利好",
        "confidence": "高",
        "beneficiary_pool": ["航天军工", "碳纤维"],
        "expectation_gap": {"level": "高", "note": "公告已出但股价未反应"},
        "reason": "涨价传导至上游",
    }


def _mock_builders():
    """mock SignalPackBuilder/ContextBuilder 的 async 构建方法(返回空结构)。"""
    spb = MagicMock()
    spb.build_for_symbols = AsyncMock(return_value={})
    cb = MagicMock()
    cb.build_symbol_contexts = AsyncMock(
        return_value={"symbols": {}, "quality_overview": {}}
    )
    return spb, cb


def _mock_sentiment_patch():
    """mock MarketSentimentCollector 三方法(慢网络源)。"""
    msc = MagicMock()
    msc.get_sentiment_summary = MagicMock(return_value={})
    msc.get_index_snapshot = MagicMock(return_value=[])
    msc.get_sector_rotation = MagicMock(return_value={})
    return patch(
        "src.collectors.market_sentiment_collector.MarketSentimentCollector",
        return_value=msc,
    )


# ============================================================
# 1. build_prompt 渲染 catalyst_analysis 段
# ============================================================

def test_build_prompt_renders_catalyst_analysis():
    agent = PremarketOutlookAgent()
    ctx = _make_context([_cn_stock()])
    data = {"catalyst_analysis": {"002361": _catalyst_result()}}
    system, user = agent.build_prompt(data, ctx)
    assert "个股事件催化与预期差" in user
    assert "002361" in user
    assert "军工材料涨价" in user
    assert "航天军工" in user
    assert "预期差:高" in user
    assert "提前潜伏" in user


def test_build_prompt_empty_catalyst_analysis_no_section():
    agent = PremarketOutlookAgent()
    ctx = _make_context([_cn_stock()])
    system, user = agent.build_prompt({}, ctx)
    assert "个股事件催化与预期差" not in user


def test_build_prompt_skips_non_dict_entries():
    agent = PremarketOutlookAgent()
    ctx = _make_context([_cn_stock()])
    data = {"catalyst_analysis": {"002361": _catalyst_result(), "bad": "not-a-dict"}}
    system, user = agent.build_prompt(data, ctx)
    assert "002361" in user


# ============================================================
# 2. collect() 采集 catalyst_analysis
# ============================================================

@pytest.mark.asyncio
async def test_collect_calls_catalyst_for_cn_symbols():
    """A股 watchlist 并发调 analyze_event_catalyst, 结果进返回 dict。"""
    agent = PremarketOutlookAgent()
    ctx = _make_context([_cn_stock("002361"), _cn_stock("600519", "贵州茅台")])

    with patch(
        "src.core.event_catalyst_engine.analyze_event_catalyst",
        side_effect=lambda sym, db=None: _catalyst_result(),
    ), patch(
        "src.agents.premarket_outlook.get_latest_analysis",
        return_value=None,
    ), patch(
        "src.core.marketdata_client.get_market_data",
        return_value=MagicMock(),
    ), patch(
        "src.agents.premarket_outlook.SignalPackBuilder",
        return_value=_mock_builders()[0],
    ), patch(
        "src.agents.premarket_outlook.ContextBuilder",
        return_value=_mock_builders()[1],
    ), patch(
        "src.collectors.wudao_mcp_client.WudaoMCPClient",
    ), _mock_sentiment_patch(), patch(
        "src.collectors.tdx_collector.collect_wenda",
        return_value={},
    ):
        data = await agent.collect(ctx)

    assert "catalyst_analysis" in data
    assert len(data["catalyst_analysis"]) == 2
    assert data["catalyst_analysis"]["002361"]["catalyst"] == "军工材料涨价"


@pytest.mark.asyncio
async def test_collect_catalyst_failure_degrades_to_empty():
    """analyze_event_catalyst 抛异常 → catalyst_analysis 为空, 不阻塞 collect。"""
    agent = PremarketOutlookAgent()
    ctx = _make_context([_cn_stock("002361")])

    def _boom(sym, db=None):
        raise RuntimeError("LLM 超时")

    with patch(
        "src.core.event_catalyst_engine.analyze_event_catalyst",
        side_effect=_boom,
    ), patch(
        "src.agents.premarket_outlook.get_latest_analysis",
        return_value=None,
    ), patch(
        "src.core.marketdata_client.get_market_data",
        return_value=MagicMock(),
    ), patch(
        "src.agents.premarket_outlook.SignalPackBuilder",
        return_value=_mock_builders()[0],
    ), patch(
        "src.agents.premarket_outlook.ContextBuilder",
        return_value=_mock_builders()[1],
    ), patch(
        "src.collectors.wudao_mcp_client.WudaoMCPClient",
    ), _mock_sentiment_patch(), patch(
        "src.collectors.tdx_collector.collect_wenda",
        return_value={},
    ):
        data = await agent.collect(ctx)

    assert data["catalyst_analysis"] == {}


@pytest.mark.asyncio
async def test_collect_skips_non_cn_symbols():
    """非 A 股(HK/US)标的不调 analyze_event_catalyst。"""
    agent = PremarketOutlookAgent()
    ctx = _make_context([
        _cn_stock("002361"),
        StockConfig(symbol="00700", name="腾讯", market=MarketCode.HK),
    ])
    calls = []

    def _track(sym, db=None):
        calls.append(sym)
        return _catalyst_result()

    with patch(
        "src.core.event_catalyst_engine.analyze_event_catalyst",
        side_effect=_track,
    ), patch(
        "src.agents.premarket_outlook.get_latest_analysis",
        return_value=None,
    ), patch(
        "src.core.marketdata_client.get_market_data",
        return_value=MagicMock(),
    ), patch(
        "src.agents.premarket_outlook.SignalPackBuilder",
        return_value=_mock_builders()[0],
    ), patch(
        "src.agents.premarket_outlook.ContextBuilder",
        return_value=_mock_builders()[1],
    ), patch(
        "src.collectors.wudao_mcp_client.WudaoMCPClient",
    ), _mock_sentiment_patch(), patch(
        "src.collectors.tdx_collector.collect_wenda",
        return_value={},
    ):
        await agent.collect(ctx)

    assert calls == ["002361"]  # 只调了 A 股
