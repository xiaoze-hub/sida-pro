"""个股因子快照（2026-10-21）：能精确算的才算，缺的留空。"""
from src.collectors.factor_daily import compute_factors
from src.core.stock_factors import render


def _bars(n, start=10.0, step=0.05, vol=1000):
    return [{"date": f"d{i}", "open": start, "high": start + i * step + 0.5,
             "low": start + i * step - 0.5, "close": start + i * step,
             "volume": vol + (i % 7) * 100} for i in range(n)]


def test_compute_core_factors():
    f = compute_factors(_bars(160))
    assert f["momentum_20"] is not None and f["momentum_60"] is not None
    assert f["vol_20"] is not None and f["vol_20"] > 0
    assert f["position_120"] is not None and 0 <= f["position_120"] <= 100
    assert f["volume_z20"] is not None


def test_insufficient_samples_return_none_not_zero():
    f = compute_factors(_bars(30))
    assert f["momentum_60"] is None, "60 日样本不足必须留空"
    assert f["position_120"] is None


def test_render_says_missing_not_fake():
    text = render({"available": True, "trade_date": "20261021",
                   "values": {"momentum_20": None, "momentum_60": None, "vol_20": None,
                              "volume_z20": None, "position_120": None},
                   "ranks": {}, "changes_20d": {}, "history_days": 1})
    assert text.count("暂无") == 5
    assert "不填近似值" in text


def test_render_marks_unready_change_window():
    text = render({"available": True, "trade_date": "20261021",
                   "values": {"momentum_20": 3.2, "momentum_60": None, "vol_20": None,
                              "volume_z20": None, "position_120": None},
                   "ranks": {"momentum_20": 78.0}, "changes_20d": {}, "history_days": 1})
    assert "分位" in text and "20 日变化需" in text
