"""P0(2026-10-18) 集合竞价量比量纲 + 代码前缀。

审计依据: docs/research/SIDA_PRO全界面UI合理性与字段排版审计重构规划.md §4.2 / §4.6 / §9-P0。
生产实测(修复前): 竞价量比 857 ~ 163,513 倍(OpenVolPre1 原值透传); 代码列 `USHT603922`。
"""
from __future__ import annotations

import math

import pytest

from src.core import auction_pool
from src.core.auction_pool import (
    AUCTION_VR_MAX,
    AUCTION_VR_MIN,
    _normalize_symbol,
    normalize_auction_volume_ratio,
)


# ── 量纲归一 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        (74898.0, 7.49),    # 生产实测第 1 行长鑫科技 → 7.49 倍
        (163513.0, 16.35),  # 生产实测第 4 行新华传媒 → 16.35 倍
        (25000.0, 2.5),
    ],
)
def test_open_vol_pre1_basis_points_are_normalized(raw, expected):
    value, label = normalize_auction_volume_ratio(raw)
    assert value == pytest.approx(expected, abs=0.01)
    assert label == "bp"


@pytest.mark.parametrize("raw", [0.5, 1.0, 2.5, 8.4, AUCTION_VR_MIN, AUCTION_VR_MAX])
def test_already_ratio_values_pass_through(raw):
    value, label = normalize_auction_volume_ratio(raw)
    assert value == pytest.approx(raw, abs=0.01)
    assert label == "ratio"


@pytest.mark.parametrize("raw", [857.0, 400000.0, 0.01, 0, -3, None, "abc", math.inf, math.nan])
def test_implausible_values_become_null_with_reason(raw):
    """落在 0.3~30 之外(或非数字)一律置空 —— 宁缺不给假量比。"""
    value, label = normalize_auction_volume_ratio(raw)
    assert value is None
    assert label in ("invalid", "out_of_range")


def test_every_normalized_value_stays_in_practical_band():
    """全表落在 0.3~30(R 级验收判据)。"""
    raw_values = [857, 1200, 5000, 74898, 163513, 25000, 2.5, 19.9]
    got = [v for v in (normalize_auction_volume_ratio(x)[0] for x in raw_values) if v is not None]
    assert got, "至少应有部分值通过归一"
    assert all(AUCTION_VR_MIN <= v <= AUCTION_VR_MAX for v in got)


def test_enhancement_loop_writes_normalized_ratio(monkeypatch):
    """端到端: TQ get_more_info 给 163513 → record.volume_ratio = 16.35。"""
    auction_pool.clear_cache()
    records = [{
        "symbol": "603922", "name": "长鑫科技", "anomaly_type": "大幅高开",
        "gap_pct": 6.2, "volume_ratio": None, "open_amount": None,
    }]
    monkeypatch.setattr(auction_pool, "_to_records", lambda df: list(records))
    monkeypatch.setitem(__import__("sys").modules, "data_source.thsdk_l2",
                        type("M", (), {"get_auction_anomaly": staticmethod(lambda m: [])}))
    monkeypatch.setattr("src.core.demon_factors.top_demon_factors", lambda n: {})
    captured = {"code": None}

    def fake_rpc(method, params):
        captured["code"] = params.get("stock_code")
        return {"OpenVolPre1": 163513, "OpenAmo": 1.2e7, "OpenZAF": 6.2}

    import marketdata.vendors.tq as tq_mod  # noqa: F401  (确认模块可导入)

    monkeypatch.setattr(tq_mod, "_rpc", fake_rpc)
    out = auction_pool.fetch_auction_anomaly("CN")
    assert captured["code"] == "603922.SH"
    assert out[0]["volume_ratio"] == pytest.approx(16.35, abs=0.01)
    assert out[0]["volume_ratio_scale"] == "bp"


# ── 代码前缀 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("USZA002361", "002361"),
        ("USHT603922", "603922"),   # 审计实测漏到页面上的沪市前缀
        ("USHA600000", "600000"),
        ("USBJ920001", "920001"),
        ("002361.SZ", "002361"),
        ("600000.SH", "600000"),
        ("sh600000", "600000"),
        ("002361", "002361"),
    ],
)
def test_normalize_symbol_strips_internal_prefix(raw, expected):
    assert _normalize_symbol(raw) == expected


def test_normalize_symbol_leaves_unknown_untouched():
    assert _normalize_symbol("881101") == "881101"
    assert _normalize_symbol("") == ""
