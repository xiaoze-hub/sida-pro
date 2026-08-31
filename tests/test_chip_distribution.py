"""筹码分布计算器测试。"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/marketdata/src"))

import pytest
from src.core.chip_distribution import compute_chips


class _Bar:
    def __init__(self, date, o, h, l, c, v):
        self.date, self.open, self.high, self.low, self.close, self.volume = date, o, h, l, c, v


def _mk_klines(n=100, base=10.0, drift=0.01):
    """构造 100 根上涨日K。"""
    out = []
    for i in range(n):
        c = base + i * drift
        out.append(_Bar(f"2026-{i:03d}", c - 0.1, c + 0.3, c - 0.3, c, 1000000))
    return out


class TestChipDistribution:
    def test_basic(self):
        r = compute_chips(_mk_klines())
        assert r is not None
        assert r["cost_10"] < r["cost_50"] < r["cost_90"]
        assert 0 <= r["profit_ratio"] <= 1
        assert r["peak_price"] > 0

    def test_uptrend_profit(self):
        """持续上涨 → 获利盘应高。"""
        r = compute_chips(_mk_klines(200, base=10, drift=0.05))
        assert r["profit_ratio"] > 0.5

    def test_insufficient_data(self):
        assert compute_chips(_mk_klines(5)) is None

    def test_real_data(self):
        """真实数据: 神剑应返回合理筹码峰。"""
        from marketdata.vendors.kline import fetch_tencent_kline_raw
        kl = fetch_tencent_kline_raw("sz002361", 300)
        if len(kl) < 50:
            pytest.skip("腾讯K线不足")
        r = compute_chips(kl)
        assert r is not None
        assert r["cost_50"] > 0
        assert 0 <= r["profit_ratio"] <= 1
