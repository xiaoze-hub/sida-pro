"""B2.5/B2.6: 指标补全(索提诺/卡玛/换手) + 年化口径统一 + 胜负互斥口径。"""

from __future__ import annotations

import math
import statistics

from src.core.backtest import metrics as M


def test_sortino_penalizes_downside_only():
    returns = [0.01, -0.02, 0.03, -0.01]
    rf = 0.0
    ppy = 252
    downside = [min(0.0, r) ** 2 for r in returns]
    dd = (sum(downside) / len(downside)) ** 0.5
    expected = statistics.fmean(returns) / dd * math.sqrt(ppy)
    assert abs(M.sortino(returns, rf, ppy) - expected) < 1e-12
    # 全为正收益 → 无下行波动 → 0.0(约定: 未定义返回 0)
    assert M.sortino([0.01, 0.02, 0.03]) == 0.0


def test_kalmar_is_annualized_over_mdd():
    curve = [100.0, 120.0, 90.0, 110.0]
    mdd = M.max_drawdown(curve)
    ann = M.annualized_return(curve)
    assert abs(M.kalmar(curve) - ann / mdd) < 1e-12
    assert M.kalmar([100.0, 100.0]) == 0.0  # 无回撤 → 0


def test_turnover():
    assert M.turnover([10_000.0, 5_000.0], 100_000.0) == 0.15
    assert M.turnover([], 100_000.0) == 0.0
    assert M.turnover([10_000.0], 0.0) == 0.0


def test_summarize_includes_new_metrics():
    curve = [100.0, 105.0, 98.0, 103.0]
    out = M.summarize(curve, [5.0, -2.0])
    assert {"sortino", "kalmar", "sharpe"} <= set(out)


def test_annualize_constant_unified():
    """B2.5: 基准模块与回测内核的年化常数必须一致(原 242 vs 252)。"""
    from src.core import portfolio_benchmark as pb

    assert pb._ANNUALIZE == M.TRADING_DAYS_PER_YEAR == 252


def test_agg_win_loss_exclusive():
    """B2.6: 同一路径"先跌后涨"的样本不再同时计入胜负。"""
    from src.core.decision_backtest import _agg

    samples = [
        {"max_gain": 8.0, "max_loss": -4.0, "close_return": 6.0},   # 赢
        {"max_gain": 5.0, "max_loss": -7.0, "close_return": -6.0},  # 亏
        {"max_gain": 2.0, "max_loss": -1.0, "close_return": 1.0},   # 都不算
    ]
    out = _agg(samples, success_pct=3.0)
    assert out["basis"] == "close_to_close"
    assert out["win_rate"] == round(1 / 3, 4)
    assert out["avg_gain"] == 6.0 and out["avg_loss"] == 6.0
    # 胜率 + 败率 ≤ 1(互斥)
    loss_rate = sum(1 for s in samples if s["close_return"] <= -3.0) / len(samples)
    assert out["win_rate"] + loss_rate <= 1.0
    # 路径极值另立字段
    assert out["path_max_gain_avg"] == round((8.0 + 5.0 + 2.0) / 3, 4)
    assert out["path_max_loss_avg"] == round((4.0 + 7.0 + 1.0) / 3, 4)
