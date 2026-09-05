"""批次C 单测: mood_cycle 容许度 / ambush_score 四维 / commodity_quotes 解析。"""
from datetime import datetime

from src.core.ambush_score import (
    build_invalidations,
    enrich_ambush_list,
    risk_deduction,
    score_event,
    score_transmission,
)
from src.core.commodity_quotes import _parse_sina_nf, momentum_score
from src.core.mood_cycle import current_mood, mood_from_phase


def _cand(**kw):
    base = {
        "symbol": "600000",
        "catalyst_date": "2026-09-10",
        "catalyst_type": "政策",
        "gap": "高",
        "reason": "某政策催化",
        "codes": [{"symbol": "600000", "via": "exact", "confidence": "高"}],
    }
    base.update(kw)
    return base


def test_mood_veto_at_climax():
    m = mood_from_phase("climax")
    assert m["available"] and m["veto"] and m["allowance"] == 0
    assert mood_from_phase("ebb")["demon_veto"] is True
    assert mood_from_phase("ignite")["allowance"] == 10


def test_mood_unknown_and_missing():
    assert mood_from_phase(None)["available"] is False
    assert mood_from_phase("xxx")["available"] is False


def test_score_event_proximity_and_type():
    today = datetime(2026, 9, 6)
    # 高预期差 + 3 天内临近 → 8+2=10
    assert score_event("政策", "高", "2026-09-08", today) == 10
    # 低预期差 + 远期 → 3-1=2
    assert score_event("政策", "低", "2026-10-01", today) == 2
    # 解禁类不因临近加分
    assert score_event("解禁", "高", "2026-09-08", today) == 8


def test_score_transmission_demon_boost():
    codes = [{"symbol": "600000", "confidence": "高"} for _ in range(3)]
    base = score_transmission(codes, None)
    boosted = score_transmission(codes, {"600000"})
    assert base == 7  # 4 + min(4, hi=3)
    assert boosted == 9  # 先锋组 +2 封顶 10
    assert score_transmission([], None) == 2


def test_risk_deduction_unlock():
    today = datetime(2026, 9, 6)
    cal = [{"type": "解禁", "symbol": "600000", "date": "2026-09-12", "title": "解禁"}]
    ded, flags = risk_deduction("600000", cal, today)
    assert ded == -3 and flags
    ded2, _ = risk_deduction("000001", cal, today)
    assert ded2 == 0


def test_build_invalidations_always_present():
    inv = build_invalidations("解禁", "2026-09-12", "因解禁压低预期")
    assert len(inv) >= 2
    assert any("解禁规模" in x for x in inv)


def test_enrich_veto_and_sort():
    today = datetime(2026, 9, 6)
    mood = mood_from_phase("climax")  # veto
    cands = [_cand(symbol="600000"), _cand(symbol="000001", gap="低", catalyst_date="2026-09-20")]
    out = enrich_ambush_list(cands, mood=mood, calendar=[], demon_symbols=None,
                             signal_lookup=lambda s: {"zjl_hb": 100.0}, today=today)
    assert len(out) == 2
    assert all(o["action"].startswith("禁推") for o in out)
    assert all(o["dims"]["mood"] == 0 for o in out)


def test_enrich_signal_missing_renormalized():
    """信号维缺数据 → 三维归一 + flags 标注(不编造)。"""
    today = datetime(2026, 9, 6)
    mood = mood_from_phase("ignite")  # 10
    out = enrich_ambush_list([_cand()], mood=mood, calendar=[], demon_symbols=None,
                             signal_lookup=lambda s: None, today=today)
    o = out[0]
    assert o["dims"]["signal"] is None
    assert any("信号维缺数据" in f for f in o["flags"])
    # event: 高8+临近4天≤7天+1=9; trans: 1个高置信=5; mood 10 → (9+5+10)/3 = 8.0
    assert o["ambush_total"] == 8.0


def test_enrich_risk_deduction_applied():
    today = datetime(2026, 9, 6)
    mood = mood_from_phase("ignite")
    cal = [{"type": "解禁", "symbol": "600000", "date": "2026-09-12", "title": "解禁"}]
    out = enrich_ambush_list([_cand()], mood=mood, calendar=cal, demon_symbols=None,
                             signal_lookup=lambda s: {"zjl_hb": -50.0}, today=today)
    o = out[0]
    # event 9 + trans 5 + mood 10 + signal 2(主力净流出) = 26/4 = 6.5, 解禁 -3 → 3.5
    assert o["ambush_total"] == 3.5
    assert any("解禁" in f for f in o["flags"])


# ---------------------------------------------------------------------------
# 新浪期货解析(口径待周一实测; 测试按社区已知格式)
# ---------------------------------------------------------------------------

SINA_SAMPLE = (
    'var hq_str_nf_SC0="原油连续,500.0,510.0,495.0,499.0,505.0,506.0,505.0,504.2,498.0,'
    '10,20,12345,原油连续,2026-09-06 09:00:00,0";'
    'var hq_str_nf_AU0="黄金连续,450.0,455.0,448.0,452.0,453.0,454.0,454.0,451.0,'
    '5,8,999,黄金连续,2026-09-06 09:00:00,0";'
)


def test_sina_nf_parse():
    items = _parse_sina_nf(SINA_SAMPLE)
    assert "SC0" in items and "AU0" in items
    sc = items["SC0"]
    assert sc["last"] == 505.0 and sc["prev_settle"] == 498.0
    assert sc["chg_pct"] > 1.0


def test_sina_nf_garbage_skipped():
    assert _parse_sina_nf('var hq_str_nf_XX="a,b,c";') == {}
    assert _parse_sina_nf("no data here") == {}


def test_momentum_score():
    closes = [100.0 + i for i in range(30)]
    assert momentum_score(closes, 20) is not None
    assert momentum_score([100.0, 101.0]) is None  # 样本不足
