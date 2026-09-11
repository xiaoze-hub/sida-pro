"""题材情绪分(2026-09-12 老板口径; 设计见 docs/research/题材情绪分_设计方案_20260912.md)。

每个通达信题材(行业 128 + 概念 457)每个交易日一个 0-100 综合情绪分:
  情绪分 = 0.28×涨停结构 + 0.24×题材扩散 + 0.20×核心强度 + 0.20×接力反馈 + 0.08×连续性
- 各维固定锚点映射 → 跨交易日可比(不做当日截面归一, 窗口/题材数量不影响排名);
- 缺失维度 → 50 中性收缩(权重不转移); 置信度独立(有效维度覆盖×成分覆盖×新鲜度);
- 涨停事实由日线 + limit_rules 自算(与 limit_up_events 同口径), 当日不依赖外部涨停池。
"""
from __future__ import annotations

WEIGHTS = {"s1": 0.28, "s2": 0.24, "s3": 0.20, "s4": 0.20, "s5": 0.08}
NEUTRAL = 50.0


def anchor_map(value, anchors) -> float:
    """分段线性夹逼 → 0-100; None/脏值 → 50 中性。anchors 按 x 升序。"""
    if value is None:
        return NEUTRAL
    try:
        v = float(value)
    except (TypeError, ValueError):
        return NEUTRAL
    if v <= anchors[0][0]:
        return float(anchors[0][1])
    if v >= anchors[-1][0]:
        return float(anchors[-1][1])
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= v <= x1:
            if x1 == x0:
                return float(y1)
            return float(y0 + (y1 - y0) * (v - x0) / (x1 - x0))
    return float(anchors[-1][1])


def percentile_rank(values, v) -> float | None:
    """v 在 values 中的百分位(0-100); 空样本/v 为 None → None(调用方按缺失处理)。"""
    xs = [float(x) for x in values if x is not None]
    if not xs or v is None:
        return None
    return 100.0 * sum(1 for x in xs if x <= float(v)) / len(xs)


_ANCH_LADDER_HEIGHT = [(1, 20), (2, 45), (3, 65), (4, 80), (6, 100)]
_ANCH_LADDER_GE2 = [(0, 0), (1, 40), (2, 60), (3, 75), (5, 100)]
_ANCH_SCALE = [(1, 35), (2, 55), (3, 68), (5, 82), (8, 92), (12, 100)]
_ANCH_SCARCE = [(0.0, 0), (0.02, 40), (0.05, 60), (0.10, 80), (0.20, 100)]
_ANCH_EXCESS_PCT = [(-3, 0), (-1, 25), (0, 50), (1, 75), (3, 100)]
_ANCH_STRONG_PP = [(-15, 0), (-8, 10), (-3, 30), (0, 50), (3, 70), (8, 90), (15, 100)]


def dim_structure(*, sealed: int, touched: int, max_boards: int, ge2: int,
                  hist_sealed: list[int], market_sealed: int) -> tuple[float | None, dict]:
    """S1 涨停结构(封住口径): 梯队40 + 规模25 + 自身历史分位20 + 同日稀缺度15。"""
    if not sealed:
        return None, {"missing": "当日无封住"}
    ladder = 0.6 * anchor_map(max_boards, _ANCH_LADDER_HEIGHT) + 0.4 * anchor_map(ge2, _ANCH_LADDER_GE2)
    scale = anchor_map(sealed, _ANCH_SCALE)
    pct = percentile_rank(hist_sealed, sealed)
    share = (sealed / market_sealed) if market_sealed else None
    scarce = anchor_map(share, _ANCH_SCARCE) if share is not None else None
    score = (
        0.40 * ladder
        + 0.25 * scale
        + 0.20 * (NEUTRAL if pct is None else pct)
        + 0.15 * (NEUTRAL if scarce is None else scarce)
    )
    detail = {
        "sealed": sealed, "touched": touched, "max_boards": max_boards, "ge2": ge2,
        "ladder": round(ladder, 2), "scale": round(scale, 2),
        "hist_pct": None if pct is None else round(pct, 2),
        "scarce": None if scarce is None else round(scarce, 2),
    }
    return score, detail


def dim_diffusion(*, pcts: list[float], market: dict) -> tuple[float | None, dict]:
    """S2 题材扩散: 中位/平均涨幅与强涨占比, 全部减全市场同口径(扣普涨)。"""
    sample = [float(p) for p in pcts if p is not None]
    if not sample:
        return None, {"missing": "无成分行情"}
    sample.sort()
    n = len(sample)
    med = sample[n // 2] if n % 2 else (sample[n // 2 - 1] + sample[n // 2]) / 2.0
    mean = sum(sample) / n
    strong_share = sum(1 for p in sample if p >= 5.0) / n * 100.0
    med_ex = med - float(market.get("pct_median") or 0.0)
    mean_ex = mean - float(market.get("pct_mean") or 0.0)
    strong_ex = strong_share - float(market.get("strong_share") or 0.0)
    score = (
        0.45 * anchor_map(med_ex, _ANCH_EXCESS_PCT)
        + 0.30 * anchor_map(mean_ex, _ANCH_EXCESS_PCT)
        + 0.25 * anchor_map(strong_ex, _ANCH_STRONG_PP)
    )
    detail = {
        "median_pct": round(med, 2), "mean_pct": round(mean, 2), "strong_share": round(strong_share, 2),
        "median_excess": round(med_ex, 2), "mean_excess": round(mean_ex, 2),
        "strong_excess_pp": round(strong_ex, 2), "sample": n,
    }
    return score, detail


_ANCH_BOARDS = [(1, 25), (2, 55), (3, 75), (4, 88), (6, 100)]
_ANCH_MOM5 = [(-10, 0), (0, 45), (5, 65), (15, 85), (30, 100)]
SEAL_QUALITY = {"一字": 1.0, "全天封死": 0.8, "开过板": 0.45}


def core_stock_score(*, boards: int, amount_pct: float | None, momentum5: float | None) -> float:
    """个股核心分 = 0.45×连板高度 + 0.30×题材内成交额分位 + 0.25×近5日动量。"""
    return (
        0.45 * anchor_map(boards, _ANCH_BOARDS)
        + 0.30 * (NEUTRAL if amount_pct is None else float(amount_pct))
        + 0.25 * anchor_map(momentum5, _ANCH_MOM5)
    )


def continuation_prob(*, boards: int, seal_quality: str, amount_pct: float | None) -> float:
    """连续概率(0-1, 展示用, 不参与总分): 连板高度 + 封板质量 + 量能分位。"""
    q = SEAL_QUALITY.get(seal_quality, 0.45)
    h = min(1.0, 0.25 * max(0, int(boards) - 1))
    a = (float(amount_pct) / 100.0) if amount_pct is not None else 0.5
    return round(max(0.05, min(0.95, 0.35 * q + 0.30 * h + 0.20 * a + 0.15)), 2)


def dim_core(candidates: list[dict]) -> tuple[float | None, dict]:
    """S3 核心强度 = 0.7×最高核心分 + 0.3×次高(仅 1 只则权重 1.0)。"""
    if not candidates:
        return None, {"missing": "无核心候选"}
    scores = sorted((float(c["core_score"]) for c in candidates), reverse=True)
    score = scores[0] if len(scores) == 1 else 0.7 * scores[0] + 0.3 * scores[1]
    detail = {"core_count": len(scores), "top": round(scores[0], 2),
              "second": round(scores[1], 2) if len(scores) > 1 else None}
    return score, detail
