"""题材情绪分测试(2026-09-12): 五维锚点映射/缺失收缩/核心判定/排序/扫描幂等。

口径唯一事实源: docs/research/题材情绪分_设计方案_20260912.md。
"""
from __future__ import annotations

import src.core.theme_mood as tm


def test_module_imports():
    assert tm.WEIGHTS == {"s1": 0.28, "s2": 0.24, "s3": 0.20, "s4": 0.20, "s5": 0.08}


def test_anchor_map_clamps_and_interpolates():
    anchors = [(0, 0), (2, 40), (5, 100)]
    assert tm.anchor_map(None, anchors) == 50.0          # 缺值 → 中性
    assert tm.anchor_map(-1, anchors) == 0.0             # 下夹逼
    assert tm.anchor_map(9, anchors) == 100.0            # 上夹逼
    assert tm.anchor_map(1, anchors) == 20.0             # 线性内插
    assert tm.anchor_map(3.5, anchors) == 70.0
    assert tm.anchor_map("x", anchors) == 50.0           # 脏值 → 中性


def test_percentile_rank_basic():
    assert tm.percentile_rank([], 3) is None             # 空样本
    assert tm.percentile_rank([1, 2, 3, 4], None) is None
    assert tm.percentile_rank([1, 2, 3, 4], 4) == 100.0
    assert tm.percentile_rank([1, 2, 3, 4], 2) == 50.0
