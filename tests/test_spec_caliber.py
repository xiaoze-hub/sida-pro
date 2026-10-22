"""数据口径契约的守卫测试（对应 specs/data/caliber.md）。"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_capital_flow_caliber_warns_against_intent_use():
    """@spec DATA-001"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    tag = CHAT_TOOL_REGISTRY["get_capital_flow"].caliber or ""
    assert tag, "资金流工具没有口径标签 —— 下游会把四档净额当主力意图"
    assert ("禁止" in tag) or ("不得" in tag), f"口径缺少红线提示: {tag!r}"


def test_directional_use_requires_tick_caliber():
    """@spec DATA-002"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY
    from src.core.caliber import CAPITAL_FLOW_TAG, CaliberViolationError, require_directional

    # 逐笔口径才可以做方向性判定
    assert "逐笔" in (CHAT_TOOL_REGISTRY["get_main_intent"].caliber or "")

    # 四档口径做方向性判定必须抛错（不是返回 False，是抛错 —— 调用方必须显式处理）
    with pytest.raises(CaliberViolationError):
        require_directional(CAPITAL_FLOW_TAG, "契约测试")


def test_kline_payload_keeps_asof_marker():
    """@spec DATA-003"""
    src = (ROOT / "src" / "web" / "api" / "klines.py").read_text(encoding="utf-8")
    assert '"as_of"' in src, "K 线响应丢了 as_of 标注 —— 昨日数据会被当今日行情"
    assert "partial" in src, "盘中合成的今日 bar 必须标 partial，否则会被当已定型"
