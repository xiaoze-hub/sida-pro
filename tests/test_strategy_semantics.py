"""P3 策略库口径修正复现测试(2026-08-23)。

- 缺失字段 = 不通过(保守语义), 缺失项记录在 missing_fields
- reversal 因子方向: 企稳(涨/平)高分, 继续跌衰减
- low_pe 对 PE<3 异常值封顶
"""
from __future__ import annotations

from src.web.api.strategies import _evaluate_strategy


def _cfg(filter_cfg: dict, ranking: dict) -> dict:
    return {"filter": filter_cfg, "ranking_factors": ranking}


class TestMissingFieldFails:
    def test_missing_volume_ratio_fails_volume_breakout(self):
        cfg = _cfg({"volume_ratio_min": 2.0}, {})
        # 行情齐全但量比缺失 → 不通过且标注 missing
        r = _evaluate_strategy(
            cfg,
            {"current_price": 10.0, "change_pct": 2.0, "volume_ratio": None},
            "volume_breakout", "600000", "CN",
        )
        assert r["passed"] is False
        assert "volume_ratio" in r["missing_fields"]

    def test_field_present_and_passing(self):
        cfg = _cfg({"volume_ratio_min": 2.0}, {})
        r = _evaluate_strategy(
            cfg,
            {"current_price": 10.0, "change_pct": 2.0, "volume_ratio": 2.5},
            "volume_breakout", "600000", "CN",
        )
        assert r["passed"] is True
        assert r["missing_fields"] == []


class TestReversalDirection:
    def test_stabilization_scores_higher_than_falling(self):
        cfg = _cfg({}, {"reversal": 1.0})
        up = _evaluate_strategy(cfg, {"current_price": 10.0, "change_pct": 0.5}, "s", "600000", "CN")
        down = _evaluate_strategy(cfg, {"current_price": 10.0, "change_pct": -6.0}, "s", "600000", "CN")
        up_factor = next(f for f in up["score_breakdown"] if f["factor"] == "reversal")
        down_factor = next(f for f in down["score_breakdown"] if f["factor"] == "reversal")
        assert up_factor["score"] == 100.0, "企稳(涨)应为满分"
        assert down_factor["score"] == 40.0, "-6% 应衰减到 40(此前方向反了会给高分)"
        assert up["score"] > down["score"]


class TestLowPeCap:
    def test_abnormal_low_pe_capped(self):
        cfg = _cfg({}, {"low_pe": 1.0})
        abnormal = _evaluate_strategy(cfg, {"current_price": 10.0, "pe_ttm": 0.1}, "s", "600000", "CN")
        normal = _evaluate_strategy(cfg, {"current_price": 10.0, "pe_ttm": 8.0}, "s", "600000", "CN")
        ab_factor = next(f for f in abnormal["score_breakdown"] if f["factor"] == "low_pe")
        nm_factor = next(f for f in normal["score_breakdown"] if f["factor"] == "low_pe")
        assert ab_factor["score"] <= 55.0, "PE=0.1(一次性收益陷阱)应被封顶"
        assert nm_factor["score"] > ab_factor["score"], "PE=8 的正常低估值应排到 PE=0.1 前面"
