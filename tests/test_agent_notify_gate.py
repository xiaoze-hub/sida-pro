"""0.3(2026-09-08): status != success 的 AnalysisResult 一律不推送(推送总闸)。

构造 status="degraded"/"failed" 的结果, 断言 5 个 agent 的 should_notify
全返回 False —— 降级文案绝不能伪装成正常分析推给用户。
"""
import pytest

from src.agents.base import AnalysisResult
from src.agents.chart_analyst import ChartAnalystAgent
from src.agents.daily_report import DailyReportAgent
from src.agents.intraday_monitor import IntradayMonitorAgent
from src.agents.news_digest import NewsDigestAgent
from src.agents.premarket_outlook import PremarketOutlookAgent

ALL_AGENTS = [
    DailyReportAgent,
    PremarketOutlookAgent,
    NewsDigestAgent,
    ChartAnalystAgent,
    IntradayMonitorAgent,
]


def _result(agent_cls, status: str) -> AnalysisResult:
    return AnalysisResult(
        agent_name=agent_cls.name,
        title=f"【{agent_cls.display_name or agent_cls.name}】生成失败",
        content="AI 分析未生成：429 限流",
        status=status,
        error="LLM 降级(daily_report): 429 限流",
    )


@pytest.mark.parametrize("agent_cls", ALL_AGENTS, ids=lambda c: c.name)
async def test_degraded_result_never_notifies(agent_cls):
    agent = agent_cls()
    assert await agent.should_notify(_result(agent_cls, "degraded")) is False


@pytest.mark.parametrize("agent_cls", ALL_AGENTS, ids=lambda c: c.name)
async def test_failed_result_never_notifies(agent_cls):
    agent = agent_cls()
    assert await agent.should_notify(_result(agent_cls, "failed")) is False


def test_degraded_result_helper_sets_status():
    """BaseAgent._degraded_result 必须产出显式 degraded 状态, content 不含降级文案伪装。"""
    agent = DailyReportAgent()
    from src.core.ai_client import LLMDegradedError

    err = LLMDegradedError("429 限流", agent.name)
    result = agent._degraded_result(err)
    assert result.status == "degraded"
    assert result.error
    assert result.agent_name == agent.name
    assert "429" in result.content
