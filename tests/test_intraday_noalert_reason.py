"""盘中监测"无需提醒"建议应包含分析内容(reason/signal)。"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

import pytest
from src.agents.intraday_monitor import IntradayMonitorAgent


@pytest.fixture(scope="module")
def agent():
    return IntradayMonitorAgent()


class TestNoAlertSuggestionHasContent:
    def test_no_alert_extracts_reason(self, agent):
        """[无需提醒] 后带原因时, reason 应提取该内容。"""
        content = "[无需提醒] 股价在均线上方运行，量能正常，暂无风险信号"
        r = agent._parse_suggestion(content)
        assert r["action"] == "hold"
        assert r["should_alert"] is False
        assert "均线上方" in r["reason"]
        assert r["signal"] == "无异常"

    def test_no_alert_without_reason_has_fallback(self, agent):
        """[无需提醒] 后无内容时, reason 应有兜底文案。"""
        content = "[无需提醒]"
        r = agent._parse_suggestion(content)
        assert r["action"] == "hold"
        assert r["reason"] == "AI 判断无需提醒"
        assert r["signal"] == "无异常"

    def test_alert_suggestion_keeps_full_content(self, agent):
        """有提醒时 signal/reason 应正常提取, 不受影响。"""
        content = "建议：减仓\n「信号」：浮亏超止损线\n「理由」：RSI超买后回落，主力流出"
        r = agent._parse_suggestion(content)
        assert r["action"] == "reduce"
        assert "止损" in r["signal"]
        assert "主力流出" in r["reason"]
        assert r["should_alert"] is True
