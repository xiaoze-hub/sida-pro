"""主力净占比量纲回归(2026-09-18)。

**怎么发现的**: 做 B5 口径留痕验证时, 看到落库的 002361「主力净占比 = 910」——
主力净流入占成交额的比例**算术上不可能超过 100%**(那意味着净流入超过当日成交额)。
实测核对(东财同源): f62 主力净流入 = 41,432,601 元, f48 成交额 = 455,662,498.5 元
→ 实测占比 = 9.0928%; 而 f184 = 909 → 909 × 0.01% = 9.09% ✅
说明 **f184 的单位是 0.01%**, 原 P1-13 的"f184 已是百分数"假设是错的(会得到 909%)。
"""
from __future__ import annotations

import logging

import pytest

from src.collectors.capital_flow_collector import _FFLOW_PCT_DIVISOR, normalize_net_pct


def test_eastmoney_f184_is_hundredth_percent():
    """实证数字钉死: f184=909 ↔ 41,432,601 / 455,662,498.5 = 9.09%"""
    net, turnover = 41_432_601.0, 455_662_498.5
    measured = net / turnover * 100
    got = normalize_net_pct(909, source="eastmoney_fflow")
    assert got is not None
    assert abs(got - measured) < 0.01, "f184 归一化后应与 净流入/成交额 一致"
    assert abs(got - 9.09) < 0.01
    assert _FFLOW_PCT_DIVISOR == 100.0


def test_gateway_path_passthrough():
    assert normalize_net_pct(9.1, source="gateway") == pytest.approx(9.1)


def test_none_stays_none():
    assert normalize_net_pct(None, source="eastmoney_fflow") is None
    assert normalize_net_pct(None, source="gateway") is None


def test_impossible_ratio_warns(caplog):
    """|占比| > 100% 是算术上不可能的 → 必须留 WARNING 便于及早发现单位回归"""
    with caplog.at_level(logging.WARNING):
        v = normalize_net_pct(12345, source="gateway")
    assert v == pytest.approx(12345.0)
    assert any("超出算术上限" in r.message for r in caplog.records)


def test_negative_and_zero_ok():
    assert normalize_net_pct(-909, source="eastmoney_fflow") == pytest.approx(-9.09)
    assert normalize_net_pct(0, source="eastmoney_fflow") == pytest.approx(0.0)
