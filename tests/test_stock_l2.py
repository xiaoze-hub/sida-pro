"""单股 L2 纯函数判定(v0.5.89): 封板质量细分 / 跳水。缺数据不猜。"""
from src.core.stock_l2 import is_dive, seal_quality_tag


def test_seal_dead_when_bid_at_limit_no_sell_pressure():
    # 买一=涨停价且有量, 卖一>涨停价(无压) → 封死
    assert seal_quality_tag([10.0], [500], [10.01], [10], 10.0, 10.0) == "封死"


def test_seal_queuing_when_sell_at_limit():
    # 买一=涨停价有量, 但卖一<=涨停价且有量(涨停价上有卖压) → 排队
    assert seal_quality_tag([10.0], [500], [10.0], [200], 10.0, 10.0) == "排队"


def test_seal_open_when_price_below_limit_and_was_sealed():
    # 现价跌破涨停价 且 今日曾封 → 开板
    assert seal_quality_tag([9.8], [100], [9.81], [50], 10.0, 9.8, ever_sealed=True) == "开板"


def test_seal_unsealed_when_price_below_limit_never_sealed():
    # 现价低于涨停价 且 从未封 → 未封
    assert seal_quality_tag([9.8], [100], [9.81], [50], 10.0, 9.8, ever_sealed=False) == "未封"


def test_seal_none_when_missing_inputs():
    assert seal_quality_tag([], [], [], [], 10.0, 10.0) is None
    assert seal_quality_tag([10.0], [500], [10.01], [10], None, 10.0) is None
    assert seal_quality_tag([10.0], [500], [10.01], [10], 10.0, None) is None


def test_is_dive_threshold():
    assert is_dive(9.7, 10.0) is True     # 跌3% ≥2%
    assert is_dive(9.85, 10.0) is False    # 跌1.5% <2%
    assert is_dive(9.8, 10.0) is True      # 正好2% 边界
    assert is_dive(None, 10.0) is None
    assert is_dive(9.8, None) is None
    assert is_dive(9.8, 0) is None
