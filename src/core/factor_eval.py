"""因子有效性评估(Phase 2):横截面 IC / IR。

回答「哪些因子在 A 股真正有 alpha」—— 把 StrategyFactorSnapshot(每个信号的因子分)
与 StrategyOutcome(前向收益)按 signal_run_id 关联,算每个因子的:
- IC(信息系数, **主口径**):按快照日做**横截面** Spearman 后取均值(B0.4, 2026-09-09);
  另附 t 统计量 ic_t 与标准差 ic_std。
- ic_pooled(**参考值**):全样本 pooled Spearman —— 混入时序变异, 可能与真实横截面
  选股能力背离, 仅作对照, 不再作为决策口径。
- IR(信息比率):IC 时序序列的 mean/std(= ic / ic_std)。
- ic_holdout:样本外段(按日期后 30%)的横截面 IC, 供因子标定的 OOS 门禁用。

纯 Python 实现相关系数(不引入 scipy/alphalens),与回测内核一致的轻量约束。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from statistics import fmean, stdev

from src.web.database import SessionLocal
from src.web.models import StrategyFactorSnapshot, StrategyOutcome

logger = logging.getLogger(__name__)

# 参与评估的因子字段(对应 StrategyFactorSnapshot 列)
FACTOR_FIELDS = (
    "alpha_score",
    "catalyst_score",
    "quality_score",
    "risk_penalty",   # 惩罚项,IC 预期为负
    "crowd_penalty",  # 惩罚项,IC 预期为负
    "final_score",
)


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson 线性相关系数;样本 < 3 或零方差返回 None。"""
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return None
    return cov / (vx ** 0.5 * vy ** 0.5)


def _rankdata(values: list[float]) -> list[float]:
    """平均秩(1-based;并列取平均)。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman 秩相关 = 对秩做 Pearson。"""
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    return pearson(_rankdata(xs), _rankdata(ys))


def evaluate_factor_ic(
    *, days: int = 90, horizon: int = 5, min_samples: int = 20, min_period_samples: int = 5,
    market: str | None = None, holdout_ratio: float = 0.0, db=None,
) -> dict:
    """计算各因子的横截面 IC / IR(+ pooled 参考值 + 样本外 IC)。

    Args:
        days: 回看快照天数
        horizon: 用哪个持有期(交易日)的 outcome
        min_samples: pooled IC 的最小样本量
        min_period_samples: 单日横截面 IC 的最小样本量
        holdout_ratio: >0 时按日期后段切出样本外段(如 0.3 = 后 30% 日期)
    """
    own = db is None
    db = db or SessionLocal()
    try:
        cutoff = (date.today() - timedelta(days=max(7, int(days)))).strftime("%Y-%m-%d")
        # 防泄漏(point-in-time):只纳入持有期已走完的样本
        # (snapshot_date + horizon 日历日 <= today),杜绝偷看未实现收益。
        horizon_cutoff = (date.today() - timedelta(days=int(horizon))).strftime("%Y-%m-%d")
        query = (
            db.query(StrategyFactorSnapshot, StrategyOutcome.outcome_return_pct)
            .join(
                StrategyOutcome,
                StrategyFactorSnapshot.signal_run_id == StrategyOutcome.signal_run_id,
            )
            .filter(
                StrategyOutcome.horizon_days == int(horizon),
                StrategyOutcome.outcome_status.in_(("evaluated", "hit_target", "hit_stop")),
                StrategyOutcome.outcome_return_pct.isnot(None),
                StrategyFactorSnapshot.snapshot_date >= cutoff,
                StrategyFactorSnapshot.snapshot_date <= horizon_cutoff,
            )
        )
        if market:
            query = query.filter(StrategyFactorSnapshot.stock_market == market)
        rows = query.all()

        all_xy: dict[str, tuple[list[float], list[float]]] = {f: ([], []) for f in FACTOR_FIELDS}
        by_date: dict[str, dict[str, tuple[list[float], list[float]]]] = {}

        for snap, ret in rows:
            try:
                r = float(ret)
            except Exception:
                continue
            for f in FACTOR_FIELDS:
                v = getattr(snap, f, None)
                if v is None:
                    continue
                try:
                    fv = float(v)
                except Exception:
                    continue
                all_xy[f][0].append(fv)
                all_xy[f][1].append(r)
                d = snap.snapshot_date or ""
                slot = by_date.setdefault(d, {}).setdefault(f, ([], []))
                slot[0].append(fv)
                slot[1].append(r)

        factors: dict[str, dict] = {}
        dates_sorted = sorted(d for d in by_date if d)
        holdout_dates: set[str] = set()
        if 0.0 < float(holdout_ratio) < 1.0 and len(dates_sorted) >= 4:
            split = int(len(dates_sorted) * (1.0 - float(holdout_ratio)))
            split = max(1, min(len(dates_sorted) - 1, split))
            holdout_dates = set(dates_sorted[split:])

        for f in FACTOR_FIELDS:
            xs, ys = all_xy[f]
            ic_pooled = spearman(xs, ys) if len(xs) >= min_samples else None
            period_ics: list[float] = []
            holdout_ics: list[float] = []
            for d, facmap in by_date.items():
                if f not in facmap:
                    continue
                pxs, pys = facmap[f]
                if len(pxs) >= min_period_samples:
                    di = spearman(pxs, pys)
                    if di is not None:
                        period_ics.append(di)
                        if d in holdout_dates:
                            holdout_ics.append(di)
            # 主口径: 横截面 IC 均值(>=3 个期才有意义)
            ic = fmean(period_ics) if len(period_ics) >= 3 else None
            ic_std = stdev(period_ics) if len(period_ics) >= 3 else None
            ic_t = None
            if ic is not None and ic_std and ic_std > 0:
                ic_t = ic / (ic_std / (len(period_ics) ** 0.5))
            ir = (ic / ic_std) if (ic is not None and ic_std and ic_std > 0) else None
            ic_holdout = fmean(holdout_ics) if len(holdout_ics) >= 2 else None
            factors[f] = {
                "ic": round(ic, 4) if ic is not None else None,
                "ic_t": round(ic_t, 4) if ic_t is not None else None,
                "ic_std": round(ic_std, 4) if ic_std is not None else None,
                "ic_pooled": round(ic_pooled, 4) if ic_pooled is not None else None,
                "ic_holdout": round(ic_holdout, 4) if ic_holdout is not None else None,
                "ir": round(ir, 4) if ir is not None else None,
                "sample_size": len(xs),
                "ic_periods": len(period_ics),
                "holdout_periods": len(holdout_ics),
            }

        return {
            "horizon": int(horizon),
            "days": int(days),
            "market": market,
            "holdout_ratio": float(holdout_ratio),
            "factors": factors,
        }
    except Exception as e:
        logger.warning(f"[因子评估] IC 计算失败: {e}")
        return {"horizon": int(horizon), "days": int(days), "market": market,
                "factors": {}, "error": str(e)}
    finally:
        if own:
            db.close()
