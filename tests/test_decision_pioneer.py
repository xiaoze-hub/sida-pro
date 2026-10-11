"""数智决策三指标纯计算单元测试(不依赖网络/DB)。"""
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


# ── 阈值一致性回归(2026-10-11 P2) ─────────────────────────────────────────
# decision_pioneer 与 ai_activity 的档位必须同源(都走 src.core.thresholds 配置层),
# env 覆盖下两者 level 恒一致 —— 钉死"两处阈值不一致"这个历史 bug。
_ENV_KEYS = ("SIDA_THRESHOLD_LIFE_LINE", "SIDA_THRESHOLD_STRONG_LINE",
             "SIDA_THRESHOLD_BULL_LINE")

# 活跃度 ≈ 3.03(默认档=强势): prev 10 → c=10.1 o=10 h=10.15 l=9.85
_LEVEL_BARS = _bars([(10, 10, 10, 10), (10, 10.15, 9.85, 10.1)])


def _both_levels():
    from src.core import ai_activity as ai
    from src.core.decision_pioneer import compute_institution_activity

    dp = compute_institution_activity(_LEVEL_BARS)
    ai_res = ai.eval_activity(_LEVEL_BARS)
    assert dp is not None
    return dp["level"], ai_res["level"]


def test_decision_pioneer_and_ai_activity_level_consistent_default(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    dp_level, ai_level = _both_levels()
    assert dp_level == ai_level == "强势"  # 3.03 落在 [3,6)


def test_decision_pioneer_and_ai_activity_level_consistent_env_override(monkeypatch):
    """env 抬高强势线 → 两模块档位同步降级(证明 decision_pioneer 真读配置层)。"""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SIDA_THRESHOLD_STRONG_LINE", "5.0")
    dp_level, ai_level = _both_levels()
    assert dp_level == ai_level, f"env 覆盖下档位不一致: dp={dp_level} ai={ai_level}"
    assert dp_level == "生命"  # 3.03 < 5.0 → 掉到生命档

    # 再抬生命线到 4.0 → 3.03 掉到"弱"
    monkeypatch.setenv("SIDA_THRESHOLD_LIFE_LINE", "4.0")
    dp_level2, ai_level2 = _both_levels()
    assert dp_level2 == ai_level2 == "弱"


def test_decision_pioneer_result_thresholds_read_config(monkeypatch):
    """返回体里的 life/strong/bull_line 是配置层生效值, 不是硬编码常量。"""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    from src.core.decision_pioneer import compute_institution_activity

    monkeypatch.setenv("SIDA_THRESHOLD_BULL_LINE", "9.9")
    r = compute_institution_activity(_LEVEL_BARS)
    assert r is not None
    assert r["bull_line"] == 9.9
    assert r["life_line"] == 1.56
    assert r["strong_line"] == 3.0


if __name__ == "__main__":
    from src.core.decision_pioneer import LIFE_LINE
    test_institution_activity_bull()
    test_institution_activity_weak()
    test_institution_activity_streak()
    test_institution_activity_insufficient()
    test_gs_signal_cross()
    test_gs_signal_insufficient()
    print("ALL PASS")
