"""决策先锋三指标纯计算单元测试(不依赖网络/DB)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.decision_pioneer import (
    LIFE_LINE,
    compute_gs_signal,
    compute_institution_activity,
)


def _bars(rows):
    """rows: [(o, h, l, c), ...] 转 bars dict 列表。"""
    return [{"open": o, "high": h, "low": l, "close": c} for o, h, l, c in rows]


def test_institution_activity_bull():
    # 前日平盘 10, 当日大涨 5% + 下影 5% → 实体+下影=10.26% ×1.2 = 12.31 大牛线
    bars = _bars([(10, 10, 10, 10), (10, 10.5, 9.5, 10.5)])
    r = compute_institution_activity(bars)
    assert r is not None
    assert r["activity"] == round(max([0, 5.263157894736842, 5.0, 5 + 5.263157894736842, 5.263157894736842, 5.0, 0.0]) * 1.2, 3)
    assert r["level"] == "大牛"
    assert r["streak_days"] == 1


def test_institution_activity_weak():
    # 两日几乎不动 → 活跃度极低, 弱
    bars = _bars([(10, 10.02, 9.98, 10), (10, 10.01, 9.99, 10)])
    r = compute_institution_activity(bars)
    assert r is not None
    assert r["activity"] < LIFE_LINE
    assert r["level"] == "弱"


def test_institution_activity_streak():
    # 连续 3 日强势(活跃度>1.56)
    bars = _bars([
        (10, 10.2, 9.9, 10.15),
        (10.15, 10.4, 10.0, 10.3),
        (10.3, 10.6, 10.2, 10.5),
        (10.5, 10.8, 10.3, 10.7),
    ])
    r = compute_institution_activity(bars)
    assert r is not None
    assert r["streak_days"] >= 3


def test_institution_activity_insufficient():
    assert compute_institution_activity([]) is None
    assert compute_institution_activity(_bars([(10, 10, 10, 10)])) is None


def test_gs_signal_cross():
    # 构造: 前段下跌(S区), 后段上涨(A0 上穿 BB0 → G)
    bars = []
    for i in range(30):
        c = 10 - i * 0.1  # 下跌
        bars.append({"open": c, "high": c + 0.05, "low": c - 0.05, "close": c})
    for i in range(10):
        c = 7 + i * 0.5  # 快速上涨
        bars.append({"open": c - 0.2, "high": c + 0.1, "low": c - 0.3, "close": c})
    r = compute_gs_signal(bars)
    assert r is not None
    assert r["signal"] == "G"
    assert r["state"] == "G区"


def test_gs_signal_insufficient():
    assert compute_gs_signal([]) is None
    assert compute_gs_signal(_bars([(10, 10, 10, 10), (10, 10, 10, 10)])) is None


if __name__ == "__main__":
    from src.core.decision_pioneer import LIFE_LINE
    test_institution_activity_bull()
    test_institution_activity_weak()
    test_institution_activity_streak()
    test_institution_activity_insufficient()
    test_gs_signal_cross()
    test_gs_signal_insufficient()
    print("ALL PASS")
