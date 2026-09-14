# -*- coding: utf-8 -*-
"""KI-057: 振幅口径 = (high-low)/prev_close×100(A股通行)。

不再用 /low(旧口径)。本文件钉住 collector 源码里的公式分支。
"""
import inspect

from src.collectors import kline_collector as kc


def test_source_uses_prev_close_denominator():
    src = inspect.getsource(kc)
    assert "prev_close" in src
    # 旧公式不得再出现
    assert "/ curr.low * 100" not in src
    assert "/ k.low * 100" not in src


def test_numeric_a_share_formula():
    """(11-9)/10*100 = 20; 旧 /low 会得到 22.22。"""
    high, low, prev_close = 11.0, 9.0, 10.0
    amp = (high - low) / prev_close * 100
    assert abs(amp - 20.0) < 1e-9
    old = (high - low) / low * 100
    assert abs(old - amp) > 1.0  # 两口径明显不同
