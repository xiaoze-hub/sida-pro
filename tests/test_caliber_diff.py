"""P2-1 口径差异解释器的钉子。

钉什么: ① **缺数不猜**(任一源缺 → unknown + 说清"无法比较");
        ② **方向冲突 → alert + 一律优先采信逐笔**(仓库硬约束);
        ③ **预期带**区隔"正常口径差"与"值得看一眼"(实测带: 暗盘/明盘 1.5~4.5×, 明盘/东财 0.5~2.0×);
        ④ **不合成单一数字**: 输出的是解释, 不带"校准后"的值。
"""
from __future__ import annotations

from src.core.caliber_diff import (
    build_pair_diffs,
    compare_pair,
    conclusion_level,
)


def test_missing_value_is_unknown_not_zero():
    """任一源缺数 → unknown, 且明说无法比较(**不拿 0 顶上拍一个差值**)。"""
    d = compare_pair("tencent_dark", "thsdk_l2", None, 12345.0)
    assert d.level == "unknown"
    assert d.abs_diff is None and d.rel_diff is None and d.ratio is None
    assert "无法比较" in d.note
    assert not d.expected


def test_expected_band_is_ok_for_dark_vs_l2():
    """暗盘 ≈ 明盘 2.75 倍是**预期**(含拆单/竞价), 不该报警。"""
    d = compare_pair("tencent_dark", "thsdk_l2", 2.75e7, 1.0e7)
    assert d.level == "ok"
    assert d.expected is True
    assert "预期带" in d.note


def test_out_of_band_but_moderate_is_warn():
    """出带但不夸张 → warn(提示先核对时间窗/基准日)。"""
    d = compare_pair("tencent_dark", "thsdk_l2", 1.1e7, 1.0e7)  # 1.1 倍, 低于下界 1.5
    assert d.level == "warn"
    assert d.expected is False


def test_gap_levels_warn_then_alert():
    """出带但接近 → warn; 远离预期带 50% 以上 → alert(先查基准日/时间窗)。"""
    near = compare_pair("tencent_dark", "thsdk_l2", 5.0e7, 1.0e7)   # 5.0× vs 带 1.5~4.5
    assert near.level == "warn" and near.expected is False
    far = compare_pair("tencent_dark", "thsdk_l2", 9.0e7, 1.0e7)    # 9.0× > 4.5×1.5
    assert far.level == "alert"


def test_l2_vs_eastmoney_tight_band():
    """明盘 L2 与东财同属"按单金额分档", 实测差约 1% → 1.1 倍必须判 ok。"""
    d = compare_pair("eastmoney_flow", "thsdk_l2", 1.1e7, 1.0e7)
    assert d.level == "ok" and d.expected is True


def test_sign_conflict_alerts_and_prefers_tick():
    """方向冲突: 一正一负 → alert + 必须标"优先采信逐笔"(不许含糊)。"""
    d = compare_pair("tencent_dark", "thsdk_l2", -8.0e6, 1.0e7)
    assert d.level == "alert"
    assert d.sign_conflict is True
    assert d.prefer == "tick"
    assert "优先采信逐笔" in d.note
    # 冲突时不报 ratio(带符号比值会失真)
    assert d.ratio is None


def test_never_returns_calibrated_value():
    """输出的字段里**没有**"校准后/权威值"这类合成数字 —— 只解释差。"""
    d = compare_pair("tencent_dark", "thsdk_l2", 2.0e7, 1.0e7).as_dict()
    for forbidden in ("calibrated", "consensus", "authoritative", "avg_value"):
        assert forbidden not in d


def test_build_pair_diffs_covers_three_pairs_and_reports_missing():
    """三对固定配对; 只给一个源时, 涉及它的对全是 unknown, 不崩。"""
    diffs = build_pair_diffs({"thsdk_l2": 1.0e7})
    assert len(diffs) == 3
    assert all(d["level"] == "unknown" for d in diffs)
    ok = build_pair_diffs({"thsdk_l2": 1.0e7, "tencent_dark": 2.75e7, "eastmoney_flow": 1.01e7})
    levels = [d["level"] for d in ok]
    assert levels[0] == "ok" and levels[1] == "ok" and levels[2] == "ok"


def test_conclusion_level_rolls_up():
    assert conclusion_level([{"level": "ok"}, {"level": "ok"}]) == "ok"
    assert conclusion_level([{"level": "ok"}, {"level": "warn"}]) == "warn"
    assert conclusion_level([{"level": "warn"}, {"level": "alert"}]) == "alert"
    assert conclusion_level([{"level": "unknown"}]) == "unknown"


def test_every_pair_has_a_human_readable_reason():
    """每对都必须给得出"为什么会差"的依据(否则用户还是只能自己猜)。"""
    for a, b in (("tencent_dark", "thsdk_l2"), ("eastmoney_flow", "thsdk_l2"), ("tencent_dark", "eastmoney_flow")):
        d = compare_pair(a, b, 2.0e7, 1.0e7)
        assert len(d.note) > 20
        assert any(k in d.note for k in ("拆单", "分档", "覆盖", "派生", "竞价"))
