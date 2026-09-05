"""双源置信度徽章单测(批次A A4, 2026-09-06 28号)。"""
from src.core.confidence import level_of, payload


def test_level_a_when_two_sources_agree():
    assert level_of(True, 5.0, 2) == "A"
    assert level_of(True, 0.0, 3) == "A"


def test_level_c_when_diverged():
    assert level_of(False, 80.0, 2) == "C"
    assert level_of(False, 120.0, 3) == "C"


def test_level_b_when_single_source():
    assert level_of(None, None, 1) == "B"


def test_none_when_no_data():
    assert level_of(None, None, 0) is None
    assert level_of(None, None, -1) is None


def test_payload_from_triangulate_shape():
    tri = {"agree": True, "consensus_wan": 123.4, "spread_pct": 3.2, "n_ok": 2,
           "sources": {"tencent": {"net_wan": 120.0}, "thsdk": {"net_wan": 125.0}}}
    p = payload(tri)
    assert p["confidence_level"] == "A"
    assert p["sources_agree"] is True
    assert p["consensus_wan"] == 123.4
    assert p["n_ok"] == 2


def test_payload_never_raises_on_garbage():
    assert payload({})["confidence_level"] is None
    p = payload({"agree": "yes", "n_ok": "two", "spread_pct": "x"})
    assert p["confidence_level"] is None
