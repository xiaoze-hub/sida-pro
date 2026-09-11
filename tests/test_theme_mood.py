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
    assert d["days"] == 2 and d["stability"] == 98.0   # 2 日样本: 100 − 2×std(1.0)
    assert round(s, 1) == round(0.6 * 61.0 + 0.4 * 98.0, 1)
    s2, d2 = tm.dim_continuity([80.0, 60.0, 60.0, 60.0])  # 只取最后 3 个
    assert d2["days"] == 3 and d2["s1_mean3"] == 60.0
    s3, d3 = tm.dim_continuity([])
    assert s3 is None and "无历史结构分" in d3["missing"]


def _today(**kw):
    base = {"members": 40, "sealed": 0, "touched": 0, "max_boards": 0, "ge2": 0,
            "sealed_syms": [], "failed_syms": [], "highest_sym": None}
    base.update(kw)
    return base


def test_total_score_neutral_keeps_weights():
    s = tm.total_score({"s1": 100.0, "s2": None, "s3": None, "s4": None, "s5": None})
    assert s == round(0.28 * 100 + 0.72 * 50, 1)      # 缺失按 50, 权重不转移


def test_confidence_and_core_rules():
    parts = {"s1": 70.0, "s2": 70.0, "s3": 70.0, "s4": 60.0, "s5": None}
    c = tm.confidence_of(parts=parts, coverage=1.0)
    # 有效权重 0.92(s5 缺失) → 0.5*0.92 + 0.3*1.0 + 0.2*1.0 = 0.96 → 96
    assert c == 96
    assert tm.is_core(score=65.0, confidence=80, recent_scores=[56, 58, 65], recent_sealed=[1, 2, 3],
                      sealed_today=3, relay=55.0) is True
    assert tm.is_core(score=65.0, confidence=80, recent_scores=[56, 58, 65], recent_sealed=[1, 2, 3],
                      sealed_today=1, relay=55.0) is False
    assert tm.is_core(score=59.0, confidence=80, recent_scores=[56, 58, 59], recent_sealed=[2, 2, 3],
                      sealed_today=3, relay=55.0) is False
    assert tm.is_core(score=65.0, confidence=69, recent_scores=[56, 58, 65], recent_sealed=[2, 2, 3],
                      sealed_today=3, relay=55.0) is False
    assert tm.is_core(score=65.0, confidence=80, recent_scores=[56, 58, 65], recent_sealed=[2, 2, 3],
                      sealed_today=3, relay=45.0) is False


def test_rank_items_tiebreakers():
    items = [
        {"block_code": "880002.SH", "score": 70.0, "score3_avg": 60.0, "confidence": 80, "limit_up_cnt": 3},
        {"block_code": "880001.SH", "score": 70.0, "score3_avg": 60.0, "confidence": 80, "limit_up_cnt": 3},
        {"block_code": "880003.SH", "score": 70.0, "score3_avg": 61.0, "confidence": 80, "limit_up_cnt": 3},
    ]
    got = [x["block_code"] for x in tm.rank_items(items)]
    assert got == ["880003.SH", "880001.SH", "880002.SH"]


def test_compute_theme_day_full_row():
    row = tm.compute_theme_day(
        date="20260911",
        today=_today(members=40, sealed=3, touched=5, max_boards=3, ge2=2),
        pcts=[3.0, 1.0, -1.0, 6.0] + [0.0] * 36,
        market={"sealed": 40, "pct_median": 1.0, "pct_mean": 1.5, "strong_share": 5.0},
        hist_sealed=[0, 1, 2],
        s1_history=[55.0, 58.0],
        sealed_history=[1, 2],
        prev={"prev_sealed": 2, "promoted": 1, "touched_today": 5, "sealed_today": 3,
              "prev_failed": 1, "failed_up": 1, "highest_pct": 3.0},
        core_candidates=[{"symbol": "600001.SH", "name": "甲", "core_score": 80.0, "prob": 0.6, "boards": 3, "pct": 10.0},
                         {"symbol": "600002.SH", "name": "乙", "core_score": 70.0, "prob": 0.5, "boards": 2, "pct": 6.0}],
    )
    assert row["trade_date"] == "20260911" and 0 <= row["score"] <= 100
    assert row["s1"] is not None and row["s3"] is not None
    assert row["core"] in (True, False) and isinstance(row["confidence"], int)
    assert len(row["core_stocks"]) == 2 and row["core_stocks"][0]["symbol"] == "600001.SH"
    assert row["breadth"]["coverage"] == 1.0 and row["limit_up_cnt"] == 3


def _mk_engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    from src.web.migrations import _m163_theme_mood_table

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with eng.begin() as conn:
        _m163_theme_mood_table(conn)
    return eng


def test_scan_upserts_and_is_idempotent(monkeypatch):
    from sqlalchemy import text as _t

    import src.db.session as dbs

    eng = _mk_engine()
    monkeypatch.setattr(dbs, "engine", eng)

    monkeypatch.setattr(tm, "sector_items", lambda: [
        {"code": "880001.SH", "name": "甲题材", "type": "concept"},
        {"code": "881001.SH", "name": "乙行业", "type": "industry"},
    ])
    monkeypatch.setattr(tm, "constituents", lambda c: ["600001.SH", "600002.SH"])
    monkeypatch.setattr(tm, "name_map", lambda: {"600001.SH": "甲股", "600002.SH": "乙股"})
    dates = ["20260909", "20260910", "20260911"]   # 首根无涨幅 → 可写日期为后两根

    def _bars(codes):
        out = {}
        for c in codes:
            bars = []
            for i, d in enumerate(dates):
                px = 10.0 + i          # 每日 +10%(10→11→12, 第二日封板/第三日炸板)
                bars.append({"date": d, "open": px * 0.99, "close": px, "high": px * 1.1, "low": px * 0.98,
                             "volume": 1000.0, "amount": 1e8})
            out[c] = bars
        return out

    monkeypatch.setattr(tm, "_fetch_bars", _bars)
    out = tm.scan(write_days=2)
    assert out["ok"] is True and out["rows"] >= 4 and out["dates"] == ["20260910", "20260911"]
    with eng.begin() as conn:
        n1 = conn.execute(_t("SELECT COUNT(*) FROM theme_mood_daily")).scalar()
    tm.scan(write_days=2)  # 幂等
    with eng.begin() as conn:
        n2 = conn.execute(_t("SELECT COUNT(*) FROM theme_mood_daily")).scalar()
    assert n1 == n2 == 4


def test_scan_failure_keeps_previous(monkeypatch):
    from sqlalchemy import text as _t

    import src.db.session as dbs

    eng = _mk_engine()
    with eng.begin() as conn:
        conn.execute(_t("INSERT INTO theme_mood_daily (trade_date, block_code, score)"
                        " VALUES ('20260910','880001.SH',66.6)"))
    monkeypatch.setattr(dbs, "engine", eng)

    def _boom():
        raise RuntimeError("tdx down")

    monkeypatch.setattr(tm, "sector_items", _boom)
    out = tm.scan(write_days=1)
    assert out["ok"] is False
    with eng.begin() as conn:
        n = conn.execute(_t("SELECT COUNT(*) FROM theme_mood_daily")).scalar()
    assert n == 1  # 旧数据保留
