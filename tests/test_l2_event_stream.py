"""批次F L2 事件流单测(纯函数部分)。"""
from src.core.l2_event_stream import (
    DARK_CLUSTER_THRESHOLD,
    evaluate_dark_cluster,
    evaluate_seal_anomaly,
)


def test_dark_cluster_hit():
    ok, detail = evaluate_dark_cluster({"net": 1_500_000.0, "buy_amt": 2_000_000.0, "sell_amt": 500_000.0})
    assert ok is True
    assert "150 万" in detail


def test_dark_cluster_below_threshold_and_missing():
    assert evaluate_dark_cluster({"net": 999_999.0})[0] is False
    assert evaluate_dark_cluster(None)[0] is False
    assert evaluate_dark_cluster({"net": None})[0] is False


def test_seal_anomaly_zscore_hit():
    m = {"available": True, "is_sealed": 1, "cancel_zscore": 2.6, "seal_quality": 0.55}
    ok, detail = evaluate_seal_anomaly(m)
    assert ok and "z=2.6" in detail


def test_seal_anomaly_quality_floor():
    m = {"available": True, "is_sealed": 1, "cancel_zscore": None, "seal_quality": 0.4}
    ok, detail = evaluate_seal_anomaly(m)
    assert ok and "偏低" in detail


def test_seal_anomaly_not_sealed_ignored():
    # 未封住(炸板后)不推封单成色异常——炸板本身是另一个信号
    m = {"available": True, "is_sealed": 0, "cancel_zscore": 5.0, "seal_quality": 0.2}
    assert evaluate_seal_anomaly(m)[0] is False
    assert evaluate_seal_anomaly({"available": False})[0] is False


def test_threshold_is_yuan():
    assert DARK_CLUSTER_THRESHOLD == 1_000_000.0  # 100万元, 金额=元红线口径
