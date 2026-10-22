# -*- coding: utf-8 -*-
"""数据口径契约的规格守卫。

对应 specs/data/caliber.md。
这三条都是出过事才写的: 把东财四档净流入当成"主力在买", 方向位与逐笔相反。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agents.chat.registry import CHAT_TOOL_REGISTRY
from src.core.caliber import (
    CAPITAL_FLOW_TAG,
    CaliberViolationError,
    require_directional,
)


def test_capital_flow_caliber_has_redline():
    """@spec DATA-001
    get_capital_flow 的 caliber 必须显式包含「禁止据此判定主力意图」类红线。
    """
    t = CHAT_TOOL_REGISTRY.get("get_capital_flow")
    assert t is not None, "get_capital_flow 未注册"
    caliber = t.caliber or ""
    assert "禁止据此判定主力意图" in caliber, (
        f"get_capital_flow 的 caliber 必须含「禁止据此判定主力意图」红线, 实际: {caliber!r}"
    )


def test_directional_requires_tick_caliber():
    """@spec DATA-002
    方向性判定只能走逐笔口径: require_directional() 对四档口径必须抛
    CaliberViolationError, 而 get_main_intent 的 caliber 必须是逐笔。
    """
    # 四档口径 → 必须抛错
    with pytest.raises(CaliberViolationError):
        require_directional(CAPITAL_FLOW_TAG, "主力意图判定")

    # 主力意图工具的 caliber 必须走逐笔
    t = CHAT_TOOL_REGISTRY.get("get_main_intent")
    assert t is not None, "get_main_intent 未注册"
    caliber = t.caliber or ""
    assert "逐笔" in caliber, (
        f"get_main_intent 的 caliber 必须说明是逐笔口径, 实际: {caliber!r}"
    )


def test_kline_reads_carry_as_of():
    """@spec DATA-003
    K 线读数必须带时点标注 as_of(盘中合成的今日 bar 另标 partial)。
    """
    klines_path = ROOT / "src" / "web" / "api" / "klines.py"
    assert klines_path.exists(), "src/web/api/klines.py 不存在"
    src = klines_path.read_text(encoding="utf-8")
    assert "as_of" in src, "K 线返回必须带 as_of 时点标注(缺了它昨日数据会被当成今日行情)"
