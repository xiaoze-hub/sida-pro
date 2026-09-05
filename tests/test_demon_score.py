"""妖股评分单测(批次B, 2026-09-06 28号)。"""
from src.core.demon_score import (
    _bucket_score,
    _streak_stats,
    demon_score_from_events,
    lianban_score,
    seal_quality_score,
    recent_activity,
)


def _ev(d: str, sealed=1, touched=1, one_way=0):
    return {"trade_date": d, "is_sealed_close": sealed, "touched": touched, "one_way": one_way}


def test_bucket_score_bands():
    assert _bucket_score(25) == 100
    assert _bucket_score(20) == 100
    assert _bucket_score(12) == 80
    assert _bucket_score(6) == 60
    assert _bucket_score(3) == 40
    assert _bucket_score(1) == 20
    assert _bucket_score(0) == 0
    assert _bucket_score(None) is None


def test_streak_stats_weekend_bridge():
    # 周五+周一(+跨周末)视为连续
    evs = [_ev("20260904"), _ev("20260907"), _ev("20260908")]
    best, runs2 = _streak_stats(evs)
    assert best == 3
    assert runs2 == 1


def test_streak_stats_gap_breaks():
    evs = [_ev("20260901"), _ev("20260920"), _ev("20260921")]
    best, runs2 = _streak_stats(evs)
    assert best == 2
    assert runs2 == 1


def test_lianban_score_with_reactivation():
    assert lianban_score(6, 1) == 100
    assert lianban_score(3, 1) == 80
    assert lianban_score(2, 3) == 70  # 60+10 反复激活
    assert lianban_score(1, 5) == 40
    assert lianban_score(0, 0) == 0
    assert lianban_score(None, None) is None


def test_seal_quality_score():
    evs = [_ev("20260901"), _ev("20260902", sealed=0), _ev("20260903", sealed=0)]
    assert seal_quality_score(evs) == 33


def test_demon_score_demon_stock():
    """20+ 次封板 → 极妖, 满频维度。"""
    evs = [_ev(f"2026{m:02d}{d:02d}") for m, d in
           [(1, 5), (1, 6), (1, 7), (2, 10), (2, 11), (2, 12), (2, 13), (2, 14), (2, 15),
            (3, 3), (3, 4), (3, 5), (4, 8), (4, 9), (4, 10), (5, 20), (5, 21), (5, 22),
            (6, 1), (6, 2)]]
    r = demon_score_from_events(evs)
    assert r["grade"] == "极妖"
    assert r["dims"]["freq"]["score"] == 100
    assert r["max_streak"] >= 6
    assert any("题材广度缺数据" in f for f in r["flags"])
    assert any("龙虎榜频次缺数据" in f for f in r["flags"])
    # freq30 + lianban20 + seal15 = 65; theme/lhb 未接入计 0; stamina=0(事件全在 60 日窗口外)
    assert r["total"] == 65.0


def test_demon_score_one_way_flag():
    evs = [_ev("20260901"), _ev("20260902"), _ev("20260903", one_way=1)]
    r = demon_score_from_events(evs)
    # Hermes 复批: 一字板改参与方式标注(排板可参与), 不写死"不可参与"
    assert r["participation"] == "一字板：排板可参与，非低吸埋伏标的"


def test_demon_score_mv_adjustment():
    evs = [_ev("20260901"), _ev("20260902"), _ev("20260903")]
    r = demon_score_from_events(evs, circ_mv=30e8)
    assert any("小盘 20-50 亿" in f for f in r["flags"])  # +8
    r2 = demon_score_from_events(evs, circ_mv=80e8)
    assert any("中盘 50-120 亿" in f for f in r2["flags"])  # +5
    r3 = demon_score_from_events(evs, circ_mv=500e8)
    assert not any("亿" in f for f in r3["flags"])


def test_demon_score_no_events():
    r = demon_score_from_events([])
    assert r["total"] is None
    assert r["grade"] == "无数据"


def test_recent_activity_window():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    d_recent = (now - timedelta(days=10)).strftime("%Y%m%d")
    d_old = (now - timedelta(days=200)).strftime("%Y%m%d")
    evs = [_ev(d_recent), _ev(d_old)]
    assert recent_activity(evs) == 1
    assert recent_activity(evs, days=300) == 2
