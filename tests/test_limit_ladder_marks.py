"""定型口径炸板/断板(spec §3.2): blown=触板未封; broken=昨>=2板今未触; 昨首板今未续≠断板。"""
from src.core.limit_ladder import finalize_marks


def _ev(d, sym, sealed):
    return {"trade_date": d, "symbol": sym, "name": f"N{sym}", "is_sealed_close": sealed}


def test_blown_is_touched_not_sealed():
    events = [_ev("20260910", "A", True), _ev("20260911", "A", False)]
    m = finalize_marks(["20260910", "20260911"], events)
    assert [x["symbol"] for x in m["20260911"]["blown"]] == ["A"]
    assert m["20260911"]["blown"][0]["prev_boards"] == 1
    assert m["20260911"]["broken"] == []


def test_broken_excludes_yesterday_first_board():
    events = [_ev("20260909", "B", True), _ev("20260909", "C", True), _ev("20260910", "C", True)]
    m = finalize_marks(["20260909", "20260910", "20260911"], events)
    # B 昨首板(0909), 0910 未续 → 断点发生在 0910, 但首板不标断板
    assert m["20260910"]["broken"] == []
    assert [x["symbol"] for x in m["20260911"]["broken"]] == ["C"]
    assert m["20260911"]["broken"][0]["prev_boards"] == 2


def test_sealed_today_neither_blown_nor_broken():
    events = [_ev("20260910", "C", True), _ev("20260911", "C", True)]
    m = finalize_marks(["20260910", "20260911"], events)
    assert m["20260911"] == {"blown": [], "broken": []}


def test_empty_events_all_empty():
    assert finalize_marks(["20260911"], []) == {"20260911": {"blown": [], "broken": []}}


def test_attach_candles_missing_is_none_not_fake():
    from src.core.limit_ladder import attach_candles

    ohlc = {("20260911", "A"): {"o": 1.0, "h": 2.0, "l": 0.5, "c": 1.8}}
    out = attach_candles([{"symbol": "A"}, {"symbol": "B"}], "20260911", ohlc)
    assert out[0]["candle"] == {"o": 1.0, "h": 2.0, "l": 0.5, "c": 1.8}
    assert out[1]["candle"] is None          # 缺数据不编影线
