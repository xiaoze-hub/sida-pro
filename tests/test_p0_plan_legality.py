"""P0(2026-10-18) 机会页可信度: 计划合法性断言 + 信号去重。

审计依据: docs/research/SIDA_PRO全界面UI合理性与字段排版审计重构规划.md §4.1 / §9-P0。
生产实测(修复前): active 30 条里 10 条 `stop_loss >= entry_high`; 30 条只对应 9 只标的。

判据(R16): `entry_low < stop_loss < target` —— 前后端各一条断言。
"""
from __future__ import annotations

import pytest

from src.core.strategy_engine import (
    PLAN_INVALID_LABEL,
    _dedupe_signal_rows,
    _format_signal,
    validate_entry_plan,
)
from src.db.models import StrategySignalRun


# ── 纯函数: 计划合法性 ────────────────────────────────────────────────


def test_valid_long_plan_passes():
    ok, reason = validate_entry_plan(
        entry_low=905.85, entry_high=924.15, stop_loss=880.0, target_price=1143.24
    )
    assert ok is True and reason == ""


def test_stop_above_entry_is_invalid():
    """寒武纪 688256 实测: 入场 905.85~924.15, 止损 938.71(挂在入场之上)。"""
    ok, reason = validate_entry_plan(
        entry_low=905.85, entry_high=924.15, stop_loss=938.71, target_price=1143.24
    )
    assert ok is False
    assert "止损" in reason and "倒挂" in reason


def test_stop_equal_to_entry_low_is_invalid():
    """止损 == 入场下沿在数学上没有风险空间(容差内一并判死)。"""
    ok, _ = validate_entry_plan(
        entry_low=10.0, entry_high=10.5, stop_loss=10.0, target_price=12.0
    )
    assert ok is False


def test_target_below_entry_is_invalid():
    ok, reason = validate_entry_plan(
        entry_low=10.0, entry_high=10.5, stop_loss=9.5, target_price=10.2
    )
    assert ok is False
    assert "目标" in reason


def test_reversed_entry_bounds_are_normalized_not_flagged():
    """生成端把 entry_low/entry_high 写反了不该误判成倒挂(按上下沿排序后再判)。"""
    ok, _ = validate_entry_plan(
        entry_low=10.5, entry_high=10.0, stop_loss=9.5, target_price=12.0
    )
    assert ok is True


def test_missing_values_are_not_contradictions():
    """缺止损/目标 = 计划不完整, 不是数学矛盾 —— 由 plan_quality/has_entry_plan 表达。"""
    assert validate_entry_plan(entry_low=None, entry_high=None, stop_loss=9.5, target_price=12.0) == (True, "")
    assert validate_entry_plan(entry_low=10.0, entry_high=10.5, stop_loss=None, target_price=None) == (True, "")


def test_single_sided_entry_uses_that_price_as_both_bounds():
    assert validate_entry_plan(entry_low=10.0, entry_high=None, stop_loss=9.5, target_price=12.0)[0] is True
    assert validate_entry_plan(entry_low=10.0, entry_high=None, stop_loss=10.5, target_price=12.0)[0] is False


# ── 读侧: 不合法计划不许显示"建仓" ──────────────────────────────────


def _row(**kw) -> StrategySignalRun:
    base = dict(
        id=1,
        snapshot_date="2026-10-17",
        stock_symbol="688256",
        stock_market="CN",
        stock_name="寒武纪",
        strategy_code="momentum",
        strategy_name="动量",
        strategy_version="v1",
        risk_level="medium",
        source_pool="market_scan",
        score=100.0,
        rank_score=100.0,
        confidence=1.0,
        status="active",
        action="buy",
        action_label="建仓",
        signal="突破",
        reason="突破",
        evidence=[],
        holding_days=3,
        entry_low=905.85,
        entry_high=924.15,
        stop_loss=938.71,
        target_price=1143.24,
        invalidation="",
        plan_quality=3,
        source_agent="strategy",
        trace_id="t",
        is_holding_snapshot=False,
        context_quality_score=80.0,
        payload={},
    )
    base.update(kw)
    return StrategySignalRun(**base)


def test_format_signal_flags_inverted_plan_and_demotes_action():
    out = _format_signal(_row(), include_payload=False)
    assert out["plan_valid"] is False
    assert out["plan_invalid_reason"]
    assert out["action"] == "watch"
    assert out["action_label"] == PLAN_INVALID_LABEL
    assert out["rank_score"] <= 65.0


def test_format_signal_keeps_valid_plan_action():
    out = _format_signal(
        _row(entry_low=900.0, entry_high=920.0, stop_loss=870.0, target_price=1100.0),
        include_payload=False,
    )
    assert out["plan_valid"] is True
    assert out["plan_invalid_reason"] == ""
    assert out["action"] == "buy"


# ── 去重: (symbol, snapshot_date, strategy) ────────────────────────────


def test_dedupe_keeps_one_row_per_symbol_strategy():
    """30 条 active 只对应 9 只标的 → 同 (symbol, date, strategy) 只留一条。"""
    rows = [
        _row(id=1, source_candidate_id=11, rank_score=100.0),
        _row(id=2, source_candidate_id=12, rank_score=92.0),
        _row(id=3, source_candidate_id=13, rank_score=88.0),
        _row(id=4, stock_symbol="300308", source_candidate_id=14, rank_score=95.0),
    ]
    kept, dropped = _dedupe_signal_rows(rows)
    assert [r.id for r in kept] == [1, 4]
    assert sorted(r.id for r in dropped) == [2, 3]


def test_dedupe_prefers_valid_plan_over_higher_score():
    """同组里出现倒挂(非法)行时, 优先保留计划合法的那条 —— 分数高也不能顶掉。"""
    rows = [
        _row(id=1, source_candidate_id=11, rank_score=100.0, stop_loss=938.71),  # 倒挂
        _row(id=2, source_candidate_id=12, rank_score=90.0, stop_loss=880.0),    # 合法
    ]
    kept, dropped = _dedupe_signal_rows(rows)
    assert [r.id for r in kept] == [2]
    assert [r.id for r in dropped] == [1]


def test_dedupe_separates_strategies_and_dates():
    rows = [
        _row(id=1, strategy_code="momentum"),
        _row(id=2, strategy_code="breakout"),
        _row(id=3, snapshot_date="2026-10-16"),
    ]
    kept, dropped = _dedupe_signal_rows(rows)
    assert len(kept) == 3 and dropped == []


@pytest.mark.parametrize("stop,target", [(938.71, 1143.24), (791.96, 965.24), (36.17, 47.14)])
def test_audit_cases_all_rejected(stop, target):
    """审计点名的三只(寒武纪/中际旭创/永鼎股份)止损都高于入场, 必须全部判非法。"""
    ok, _ = validate_entry_plan(entry_low=10.0, entry_high=10.2, stop_loss=stop, target_price=target)
    assert ok is False
