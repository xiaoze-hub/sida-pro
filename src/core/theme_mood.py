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
