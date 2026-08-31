"""第二批 AI 工具注册的单元测试(2026-08-22)。

覆盖 get_event_catalyst / get_intent_explain / get_factor_ic_report 三个
新对话工具的 _execute_tool 分派: 成功渲染 / market!=CN 拒绝 / 失败降级。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio

from src.web.api.chat import _execute_tool


@pytest.fixture
def mock_db():
    return MagicMock()


# ──────────────────────────────────────────────────────────
# get_event_catalyst
# ──────────────────────────────────────────────────────────
@patch("src.core.event_catalyst_engine.analyze_event_catalyst")
async def test_event_catalyst_success(mock_analyze, mock_db):
    mock_analyze.return_value = {
        "catalyst": "半导体涨价",
        "direction": "利好",
        "confidence": "高",
        "beneficiary_pool": ["芯片设计", "封测", "设备"],
        "expectation_gap": {"level": "高", "note": "公告已出但股价未反应"},
        "reason": "涨价传导至上游",
    }
    text = await _execute_tool(mock_db, "get_event_catalyst", {"symbol": "002361"})
    assert "[数据源: 当日公告→AI推理]" in text
    assert "半导体涨价" in text
    assert "芯片设计" in text
    assert "预期差: 高" in text


@patch("src.core.event_catalyst_engine.analyze_event_catalyst", return_value=None)
async def test_event_catalyst_no_events(mock_analyze, mock_db):
    text = await _execute_tool(mock_db, "get_event_catalyst", {"symbol": "002361"})
    assert "无公告事件" in text or "无法生成" in text


async def test_event_catalyst_market_not_cn(mock_db):
    text = await _execute_tool(mock_db, "get_event_catalyst", {"symbol": "AAPL", "market": "US"})
    assert "仅支持 A 股" in text


# ──────────────────────────────────────────────────────────
# get_intent_explain
# ──────────────────────────────────────────────────────────
@patch("src.core.intent_explain.explain_main_intent")
@patch("src.core.dark_flow.compute_dark_flow")
async def test_intent_explain_success(mock_dark, mock_explain, mock_db):
    mock_dark.return_value = {"signal": "主力净流入(吸筹)", "data_status": "ok"}
    mock_explain.return_value = {"direction": "吸筹", "confidence": "高", "why": "超大单+5967万但大单-8433万"}
    text = await _execute_tool(mock_db, "get_intent_explain", {"symbol": "002361"})
    assert "吸筹" in text
    assert "超大单" in text
    assert "主力意图规则算法" in text


@patch("src.core.dark_flow.compute_dark_flow", return_value=None)
async def test_intent_explain_no_dark(mock_dark, mock_db):
    text = await _execute_tool(mock_db, "get_intent_explain", {"symbol": "002361"})
    assert "未能获取" in text


async def test_intent_explain_market_not_cn(mock_db):
    text = await _execute_tool(mock_db, "get_intent_explain", {"symbol": "AAPL", "market": "US"})
    assert "仅支持 A 股" in text


# ──────────────────────────────────────────────────────────
# get_factor_ic_report
# ──────────────────────────────────────────────────────────
@patch("src.core.factor_ic_report.generate_factor_ic_report")
async def test_factor_ic_report_success(mock_gen, mock_db):
    mock_gen.return_value = {
        "summary": "alpha_score 与 catalyst_score 有真实 alpha",
        "factor_assessment": [
            {"factor_code": "alpha_score", "assessment": "有效", "note": "IC=0.12 样本充足"},
            {"factor_code": "crowd_penalty", "assessment": "存疑", "note": "IC≈0"},
        ],
        "adjustment_suggestion": "可考虑上调 alpha_score 权重",
        "confidence": "高",
    }
    text = await _execute_tool(mock_db, "get_factor_ic_report", {"market": "CN"})
    assert "[数据源: 因子IC/IR评估" in text
    assert "alpha_score" in text
    assert "真实 alpha" in text
    assert "调权建议" in text


@patch("src.core.factor_ic_report.generate_factor_ic_report", return_value=None)
async def test_factor_ic_report_no_data(mock_gen, mock_db):
    text = await _execute_tool(mock_db, "get_factor_ic_report", {"market": "CN"})
    assert "样本不足" in text or "无法生成" in text


async def test_factor_ic_report_market_not_cn(mock_db):
    text = await _execute_tool(mock_db, "get_factor_ic_report", {"market": "US"})
    assert "仅支持 A 股" in text
