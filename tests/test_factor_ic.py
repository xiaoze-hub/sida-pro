"""因子 IC（2026-10-21）：本地 Spearman，不需要外部调仓数据；样本不足要明说。"""
from src.core.factor_ic import _spearman, render


def test_spearman_perfect_and_inverse():
    xs = list(range(12))
    assert _spearman(xs, xs) == 1.0
    assert round(_spearman(xs, [-x for x in xs]), 4) == -1.0


def test_spearman_requires_enough_samples():
    assert _spearman([1, 2, 3], [1, 2, 3]) is None


def test_render_reports_insufficient_samples():
    text = render({"available": True, "horizon": 5, "dates": ["20261001", "20261020"],
                   "factors": {"momentum_20": {"label": "20 日动量", "ic_mean": None, "days": 2}}})
    assert "样本不足" in text and "至少需 5 个" in text


def test_render_marks_local_caliber():
    text = render({"available": True, "horizon": 5, "dates": ["20260901", "20261020"],
                   "factors": {"momentum_20": {"label": "20 日动量", "ic_mean": 0.031,
                                               "ir": 0.6, "win_rate": 58.0, "days": 20}}})
    assert "IC 均值 +0.031" in text and "未使用任何外部调仓数据" in text
