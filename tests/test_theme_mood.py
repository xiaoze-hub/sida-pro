"""题材情绪分测试(2026-09-12): 五维锚点映射/缺失收缩/核心判定/排序/扫描幂等。

口径唯一事实源: docs/research/题材情绪分_设计方案_20260912.md。
"""
from __future__ import annotations

import src.core.theme_mood as tm


def test_module_imports():
    assert tm.WEIGHTS == {"s1": 0.28, "s2": 0.24, "s3": 0.20, "s4": 0.20, "s5": 0.08}
