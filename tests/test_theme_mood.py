"""题材情绪分测试(2026-09-12): 五维锚点映射/缺失收缩/核心判定/排序/扫描幂等。

口径唯一事实源: docs/research/题材情绪分_设计方案_20260912.md。
"""
from __future__ import annotations

import src.core.theme_mood as tm


def test_module_imports():
    assert tm.WEIGHTS == {"s1": 0.28, "s2": 0.24, "s3": 0.20, "s4": 0.20, "s5": 0.08}


def test_anchor_map_clamps_and_interpolates():
    anchors = [(0, 0), (2, 40), (5, 100)]
    assert tm.anchor_map(None, anchors) == 50.0          # 缺值 → 中性
    assert tm.anchor_map(-1, anchors) == 0.0             # 下夹逼
    assert tm.anchor_map(9, anchors) == 100.0            # 上夹逼
    assert tm.anchor_map(1, anchors) == 20.0             # 线性内插
    assert tm.anchor_map(3.5, anchors) == 70.0
    assert tm.anchor_map("x", anchors) == 50.0           # 脏值 → 中性


def test_percentile_rank_basic():
    assert tm.percentile_rank([], 3) is None             # 空样本
    assert tm.percentile_rank([1, 2, 3, 4], None) is None
    assert tm.percentile_rank([1, 2, 3, 4], 4) == 100.0
    assert tm.percentile_rank([1, 2, 3, 4], 2) == 50.0


def test_dim_structure_neutral_when_no_sealed():
    s, d = tm.dim_structure(sealed=0, touched=3, max_boards=0, ge2=0, hist_sealed=[0, 1], market_sealed=40)
    assert s is None and "无封住" in d["missing"]


def test_dim_structure_scores_and_subitem_missing():
    s, d = tm.dim_structure(sealed=3, touched=5, max_boards=3, ge2=2, hist_sealed=[0, 1, 2, 3, 4], market_sealed=40)
    # ladder = 0.6*65 + 0.4*60 = 63; scale(3)=68; hist_pct(3/5)=80; scarce(3/40=7.5%)=70
    assert round(s, 1) == round(0.40 * 63 + 0.25 * 68 + 0.20 * 80 + 0.15 * 70, 1)
    s2, d2 = tm.dim_structure(sealed=2, touched=2, max_boards=2, ge2=1, hist_sealed=[], market_sealed=0)
    # 历史/稀缺缺失 → 各自按 50 收缩, 权重不转移
    assert round(s2, 1) == round(0.40 * (0.6 * 45 + 0.4 * 40) + 0.25 * 55 + 0.20 * 50 + 0.15 * 50, 1)
    assert d2["hist_pct"] is None and d2["scarce"] is None


def test_dim_diffusion_subtracts_market():
    market = {"pct_median": 1.0, "pct_mean": 1.5, "strong_share": 5.0}
    s, d = tm.dim_diffusion(pcts=[3.0, 1.0, -1.0, 6.0], market=market)
    # 中位 2.0(超额+1.0→75); 均值 2.25(超额+0.75→68.75); 强涨占比 25%(超额+20pp→上夹逼100)
    assert d["median_excess"] == 1.0 and d["strong_excess_pp"] == 20.0
    assert round(s, 2) == round(0.45 * 75 + 0.30 * 68.75 + 0.25 * 100, 2)
    s2, d2 = tm.dim_diffusion(pcts=[], market=market)
    assert s2 is None and "无成分行情" in d2["missing"]


def test_core_stock_score_and_prob():
    hi = tm.core_stock_score(boards=3, amount_pct=100.0, momentum5=15.0)
    lo = tm.core_stock_score(boards=1, amount_pct=0.0, momentum5=-5.0)
    assert hi > lo and 0 <= lo <= 100 and 0 <= hi <= 100
    assert tm.continuation_prob(boards=1, seal_quality="开过板", amount_pct=10.0) < \
           tm.continuation_prob(boards=4, seal_quality="一字", amount_pct=90.0)
    assert 0 < tm.continuation_prob(boards=1, seal_quality="开过板", amount_pct=None) < 1


def test_dim_core_top2_weighting():
    cands = [{"core_score": 80.0}, {"core_score": 60.0}, {"core_score": 40.0}]
    s, d = tm.dim_core(cands)
    assert round(s, 2) == round(0.7 * 80 + 0.3 * 60, 2)
    assert d["core_count"] == 3
    s1, d1 = tm.dim_core([{"core_score": 70.0}])
    assert s1 == 70.0 and d1["second"] is None
    s0, d0 = tm.dim_core([])
    assert s0 is None and "无核心候选" in d0["missing"]


def test_dim_relay_subitems_and_missing():
    s, d = tm.dim_relay(prev_sealed=4, promoted=2, touched_today=6, sealed_today=3,
                        prev_failed=2, failed_up=1, highest_pct=5.0)
    assert d["promote_rate"] == 50.0 and d["seal_rate"] == 50.0 and d["carry_rate"] == 50.0
    assert round(s, 1) == round(0.35 * 70 + 0.30 * 50 + 0.20 * 55 + 0.15 * 75, 1)
    s2, d2 = tm.dim_relay(prev_sealed=0, promoted=0, touched_today=0, sealed_today=0,
                          prev_failed=0, failed_up=0, highest_pct=None)
    assert s2 is None and "无昨日样本" in d2["missing"]


def test_dim_continuity_uses_prior_3_days():
    s, d = tm.dim_continuity([60.0, 62.0])
    assert d["days"] == 2 and d["stability"] == 50.0
    assert round(s, 1) == round(0.6 * 61.0 + 0.4 * 50.0, 1)
    s2, d2 = tm.dim_continuity([80.0, 60.0, 60.0, 60.0])  # 只取最后 3 个
    assert d2["days"] == 3 and d2["s1_mean3"] == 60.0
    s3, d3 = tm.dim_continuity([])
    assert s3 is None and "无历史结构分" in d3["missing"]
