"""影子回测 + 差值归因(移植自 HKUDS/Vibe-Trading, MIT,按数智分析 A 股收敛)。

归因分解(signed —— 正值 = 影子相对赚更多):
    noise_trades_pnl   = -Σ 违反任何规则的真实交易累计 PnL(情绪单)
    early_exit_pnl     = +Σ 赢单但持仓 < 规则下限的机会成本(按不足比例折算)
    late_exit_pnl      = +Σ 亏单但持仓 > 规则上限的放大损失(按超额比例折算)
    overtrading_pnl    = -Σ 超出影子预期交易频率的真实交易 PnL
    missed_signals_pnl = 残差 (shadow_pnl − real_pnl − 前四项之和)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

from src.core.shadow_account.journal import pair_trades_fifo
from src.core.shadow_account.models import (
    AttributionBreakdown,
    ShadowBacktestResult,
    ShadowProfile,
)
from src.core.shadow_account.parsers import parse_file, records_to_dataframe

logger = logging.getLogger(__name__)


def run_shadow_attribution(
    profile: ShadowProfile,
    journal_path: str | Path,
) -> tuple[AttributionBreakdown, float, float]:
    """计算影子 PnL 与用户真实 PnL 的差值归因。

    Args:
        profile: 已提取的 ShadowProfile(规则即影子)。
        journal_path: 原始交割单路径(用于用户真实回合)。

    Returns:
        (attribution, shadow_total_pnl, real_total_pnl)。

    Notes:
        shadow_total_pnl 在 A 股收敛版里 = 用户真实回合中"符合影子规则"的
        回合 PnL 之和(规则一致性收益,而非独立行情回测)。完整 Vibe 版还做
        多市场历史回测;数智分析聚焦"规则违背/过早离场/错过信号"归因。
    """
    path = Path(journal_path)
    if not path.exists():
        return _zero_attribution(), 0.0, 0.0
    try:
        _, records = parse_file(path)
        trades_df = records_to_dataframe(records)
        roundtrips = pair_trades_fifo(trades_df)
    except Exception as exc:
        logger.warning("Attribution skipped — journal parse failed: %s", exc)
        return _zero_attribution(), 0.0, 0.0
    if not roundtrips:
        return _zero_attribution(), 0.0, 0.0

    return _compute_attribution(profile=profile, roundtrips=roundtrips)


def _zero_attribution() -> AttributionBreakdown:
    return AttributionBreakdown(
        missed_signals_pnl=0.0,
        noise_trades_pnl=0.0,
        early_exit_pnl=0.0,
        late_exit_pnl=0.0,
        overtrading_pnl=0.0,
        counterfactual_trades=(),
    )


def _compute_attribution(
    *,
    profile: ShadowProfile,
    roundtrips: list[dict[str, Any]],
) -> tuple[AttributionBreakdown, float, float]:
    """归因用户真实 PnL 与影子 PnL 的差值。

    counterfactual_trades 列出 |impact| 前 5 的回合(报告 Section 6)。
    """
    rule_hold_lo, rule_hold_hi = _aggregate_holding_range(profile)
    noise = 0.0
    early = 0.0
    late = 0.0
    shadow_pnl = 0.0
    real_pnl = 0.0
    counterfactuals: list[dict[str, Any]] = []

    for rt in roundtrips:
        pnl = float(rt["pnl"])
        real_pnl += pnl
        hold = float(rt["hold_days"])
        within_rule = rule_hold_lo <= hold <= rule_hold_hi
        if within_rule:
            shadow_pnl += pnl  # 符合影子规则的回合计入"影子 PnL"
        impact = 0.0
        reason = ""
        if not within_rule:
            noise += -pnl
            impact += -pnl
            reason = "rule_violation"
        if pnl > 0 and hold < rule_hold_lo:
            shortfall = pnl * max(0.0, (rule_hold_lo - hold) / max(rule_hold_lo, 1))
            early += shortfall
            impact += shortfall
            reason = reason or "early_exit"
        if pnl < 0 and hold > rule_hold_hi:
            excess = -pnl * max(0.0, (hold - rule_hold_hi) / max(rule_hold_hi, 1))
            late += excess
            impact += excess
            reason = reason or "late_exit"
        if impact != 0.0:
            counterfactuals.append({
                "symbol": rt["symbol"],
                "buy_dt": str(rt["buy_dt"]),
                "sell_dt": str(rt["sell_dt"]),
                "hold_days": hold,
                "pnl": round(pnl, 2),
                "impact": round(impact, 2),
                "reason": reason,
            })

    overtrading = _overtrading_pnl(profile=profile, roundtrips=roundtrips)
    explained = noise + early + late + overtrading
    missed = round(shadow_pnl - real_pnl - explained, 2)

    counterfactuals.sort(key=lambda r: abs(r["impact"]), reverse=True)
    top5 = tuple(counterfactuals[:5])

    return (
        AttributionBreakdown(
            missed_signals_pnl=round(missed, 2),
            noise_trades_pnl=round(noise, 2),
            early_exit_pnl=round(early, 2),
            late_exit_pnl=round(late, 2),
            overtrading_pnl=round(overtrading, 2),
            counterfactual_trades=top5,
        ),
        round(shadow_pnl, 2),
        round(real_pnl, 2),
    )


def _aggregate_holding_range(profile: ShadowProfile) -> tuple[float, float]:
    """并集所有规则的持仓天数范围(lo=min, hi=max)。"""
    if not profile.rules:
        return (1.0, 30.0)
    los = [r.holding_days_range[0] for r in profile.rules]
    his = [r.holding_days_range[1] for r in profile.rules]
    return (float(min(los)), float(max(his)))


def _overtrading_pnl(
    *,
    profile: ShadowProfile,
    roundtrips: list[dict[str, Any]],
) -> float:
    """超频交易 PnL: 超出影子预期交易预算的回合。

    影子约每 2 * median_hold_days 天交易 1 次;与实际回合数对比。
    超出部分的 PnL 以负号计入(影子会跳过它们 —— 真实 PnL 无论正负都是噪音)。
    """
    if not roundtrips:
        return 0.0
    median_hold, _ = profile.typical_holding_days
    if median_hold <= 0:
        return 0.0
    span_days = (
        pd.Timestamp(roundtrips[-1]["sell_dt"]) - pd.Timestamp(roundtrips[0]["buy_dt"])
    ).total_seconds() / 86400.0
    expected = max(1.0, span_days / max(2 * median_hold, 1.0))
    actual = len(roundtrips)
    if actual <= expected:
        return 0.0
    # 惩罚 |pnl| 最小的"额外"回合 —— 它们看起来像噪音
    extras = sorted(roundtrips, key=lambda rt: abs(float(rt["pnl"])))
    extra_count = int(actual - expected)
    extra_pnl = sum(float(rt["pnl"]) for rt in extras[:extra_count])
    return -extra_pnl


def summarize_result(
    profile: ShadowProfile,
    attribution: AttributionBreakdown,
    shadow_pnl: float,
    real_pnl: float,
) -> ShadowBacktestResult:
    """组装 ShadowBacktestResult(单市场 A 股,无多币种池)。"""
    return ShadowBacktestResult(
        shadow_id=profile.shadow_id,
        per_market={"china_a": {"shadow_pnl": round(shadow_pnl, 2), "real_pnl": round(real_pnl, 2)}},
        combined={
            "shadow_pnl": round(shadow_pnl, 2),
            "real_pnl": round(real_pnl, 2),
            "delta_pnl": round(shadow_pnl - real_pnl, 2),
        },
        equity_curves={},
        attribution=attribution,
        shadow_total_pnl=round(shadow_pnl, 2),
        real_total_pnl=round(real_pnl, 2),
        delta_pnl=round(shadow_pnl - real_pnl, 2),
    )
