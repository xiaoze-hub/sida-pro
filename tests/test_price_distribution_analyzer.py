# -*- coding: utf-8 -*-
"""I-8 分价博弈矩阵算法测试(2026-10-18)。

不触网: 全部走 `build_matrix` / detect_* 纯函数与 `fetch_analysis` 的注入式取数。
守四件事:
  ① **口径可复算**: 竞买率/主动卖量/净额/全日占比逐值锁死, 换实现就红;
  ② **托举/抛压只认放量前列**: 缩量高竞买率不算支撑(噪声), 成交量前 3 之外的档一律不进;
  ③ **拆单判据不许凭空**: 源没给大单列 → 空; 大单占比不显著低于当日水平 → 空;
  ④ **降级诚实**: 档位不足/取数异常 → available=False + reason 原文, 不返回 0 或估算值。
"""
from __future__ import annotations

import pytest

from src.core.price_distribution_analyzer import (
    MIN_BINS,
    build_matrix,
    build_summary,
    detect_iceberg_signals,
    detect_resistance_levels,
    detect_support_levels,
    fetch_analysis,
    normalize_bin,
)


def _bin(price: float, buy: float, total: float, big: float = 0.0) -> dict:
    return {
        "price": price,
        "buy_vol": buy,
        "total_vol": total,
        "counter_vol": total - buy - big,
        "big_vol": big,
    }


def _payload(rows, *, date="2026-10-16", ts="15:00:02", ok=None) -> dict:
    return {
        "trade_date": date,
        "snapshot_ts": ts,
        "ticks": 1000,
        "bins": rows,
        "identity_ok": len(rows) if ok is None else ok,
        "identity_bad": 0,
    }


# --------------------------------------------------------------------------- #
# 单档归一化
# --------------------------------------------------------------------------- #
def test_normalize_bin_computes_ratio_sell_net_and_share():
    row = normalize_bin(_bin(10.0, 7800, 10000, big=500), day_vol=40000)
    assert row is not None
    assert row["buy_ratio"] == pytest.approx(0.78)
    assert row["sell_ratio"] == pytest.approx(0.22)
    assert row["sell_vol"] == pytest.approx(2200)
    assert row["net_vol"] == pytest.approx(5600)
    # 净额 = 净量(手) × 100 股 × 价
    assert row["net_amount"] == pytest.approx(5600 * 100 * 10.0)
    assert row["day_share"] == pytest.approx(0.25)
    assert row["big_ratio"] == pytest.approx(0.05)


def test_normalize_bin_clamps_dirty_buy_volume_without_silently_fixing():
    row = normalize_bin(_bin(10.0, 12000, 10000), day_vol=10000)
    assert row["buy_vol"] == pytest.approx(10000)
    assert row["buy_ratio"] == pytest.approx(1.0)
    assert row["sell_vol"] == pytest.approx(0.0)
    assert row["clamped"] is True  # 夹紧要留痕, 不许静默改数


def test_normalize_bin_drops_rows_without_price_or_volume():
    assert normalize_bin({"price": None, "total_vol": 100}) is None
    assert normalize_bin({"price": 10.0, "total_vol": 0}) is None


# --------------------------------------------------------------------------- #
# 托举 / 抛压
# --------------------------------------------------------------------------- #
def _mixed_bins():
    """成交量排序: 10.00(托举) > 10.10(中性) > 10.20(中性) > 9.90(缩量高竞买)。"""
    return [
        normalize_bin(_bin(10.00, 8000, 10000), 40000),   # 前1 量, 竞买 80%
        normalize_bin(_bin(10.10, 5000, 9000), 40000),    # 前2 量, 中性
        normalize_bin(_bin(10.20, 4500, 8000), 40000),    # 前3 量, 中性
        normalize_bin(_bin(9.90, 1900, 2000), 40000),     # 缩量, 竞买 95%
        normalize_bin(_bin(9.80, 400, 1000), 40000),
        normalize_bin(_bin(10.30, 450, 1000), 40000),
    ]


def test_support_requires_top_volume_and_dominant_buy():
    levels = detect_support_levels(_mixed_bins())
    prices = [lv["price"] for lv in levels]
    assert prices == [10.00]          # 9.90 竞买 95% 但只成交 2000 手 → 不算托举
    assert levels[0]["buy_ratio"] == pytest.approx(0.8)


def test_resistance_requires_top_volume_and_dominant_sell():
    bins = [
        normalize_bin(_bin(10.0, 1500, 10000), 30000),   # 前1 量, 竞买 15% → 抛压
        normalize_bin(_bin(10.1, 5000, 9000), 30000),
        normalize_bin(_bin(10.2, 5000, 8000), 30000),
        normalize_bin(_bin(10.3, 100, 500), 30000),      # 量小, 竞买 20% → 不算抛压
        normalize_bin(_bin(10.4, 300, 1000), 30000),
        normalize_bin(_bin(10.5, 400, 1000), 30000),
    ]
    levels = detect_resistance_levels(bins)
    assert [lv["price"] for lv in levels] == [10.0]
    assert levels[0]["sell_ratio"] == pytest.approx(0.85)


# --------------------------------------------------------------------------- #
# 拆单
# --------------------------------------------------------------------------- #
def _iceberg_bins(big_vol: float, *, big_ratio_ref: float | None):
    """巨量档(30000 手) + 5 个 5000 手小档; 只有巨量档的 big_vol 可调。"""
    rows = [
        normalize_bin(_bin(32.10, 24000, 30000, big=big_vol), 55000),
        *[normalize_bin(_bin(32.00 + i * 0.01, 2500, 5000, big=250), 55000) for i in range(5)],
    ]
    return rows


def test_iceberg_detects_small_order_split_buying():
    rows = _iceberg_bins(0, big_ratio_ref=0.05)
    out = detect_iceberg_signals(rows, day_vol=55000, big_data_present=True, day_big_ratio=0.05)
    assert len(out) == 1
    assert out[0]["price"] == pytest.approx(32.10)
    assert out[0]["side"] == "buy"
    assert out[0]["label"] == "算法拆单买入(吸筹)"
    assert out[0]["big_ratio"] == pytest.approx(0.0)


def test_iceberg_direction_flips_when_sell_dominant():
    rows = [
        normalize_bin(_bin(32.10, 3000, 30000), 55000),  # 竞买 10% → 拆单派发
        *[normalize_bin(_bin(32.00 + i * 0.01, 2500, 5000), 55000) for i in range(5)],
    ]
    out = detect_iceberg_signals(rows, day_vol=55000, big_data_present=True, day_big_ratio=0.05)
    assert out and out[0]["side"] == "sell"
    assert out[0]["label"] == "算法拆单卖出(派发)"


def test_iceberg_requires_big_column_present():
    rows = _iceberg_bins(0, big_ratio_ref=None)
    assert detect_iceberg_signals(rows, day_vol=55000, big_data_present=False) == []


def test_iceberg_rejects_level_that_has_big_orders():
    """巨量档若本身有大单成交(占比不低于当日水平), 就不是拆单, 不能误报。"""
    rows = _iceberg_bins(6000, big_ratio_ref=0.2)  # 20% 大单
    assert detect_iceberg_signals(rows, day_vol=55000, big_data_present=True, day_big_ratio=0.2) == []


def test_iceberg_rejects_balanced_direction():
    """方向不明(竞买率 50% 附近)只是多空拉锯, 不构成"拆单吸筹/派发"。"""
    rows = [
        normalize_bin(_bin(32.10, 15500, 30000), 55000),
        *[normalize_bin(_bin(32.00 + i * 0.01, 2500, 5000), 55000) for i in range(5)],
    ]  # 30000 手 × 32.10 × 100 ≈ 9630 万元, 过经济规模闸
    assert detect_iceberg_signals(rows, day_vol=55000, big_data_present=True, day_big_ratio=0.05) == []


def test_iceberg_requires_volume_outlier():
    """量不够"巨量"(未达 3×中位量)的小档不判拆单。"""
    rows = [normalize_bin(_bin(32.0, 6000, 9000), 40000)] + [
        normalize_bin(_bin(32.1 + i * 0.01, 1500, 5000), 40000) for i in range(5)
    ]
    assert detect_iceberg_signals(rows, day_vol=40000, big_data_present=True, day_big_ratio=0.05) == []


# --------------------------------------------------------------------------- #
# 结论文案
# --------------------------------------------------------------------------- #
def test_summary_text_matches_product_contract():
    support = [{"price": 34.62, "buy_ratio": 0.785, "volume": 12000}]
    resistance = [{"price": 35.80, "sell_ratio": 0.821, "buy_ratio": 0.179, "volume": 9000}]
    iceberg = [{"price": 35.32, "side": "buy", "volume": 18000, "big_ratio": 0.004, "buy_ratio": 0.72}]
    summary = build_summary(support, resistance, iceberg)
    assert summary[0]["text"] == "34.62 (主动买占比 78.5%)"
    assert summary[1]["text"] == "35.80 (主动卖占比 82.1%)"
    assert summary[2]["text"].startswith("在 35.32 附近检测到小单高频拆单吸筹")
    assert [s["key"] for s in summary] == ["support", "resistance", "iceberg"]


def test_summary_does_not_invent_levels_when_nothing_hits():
    summary = build_summary([], [], [])
    assert all(s["available"] is False for s in summary)
    assert all("未检测到" in s["text"] for s in summary)


# --------------------------------------------------------------------------- #
# 矩阵整体
# --------------------------------------------------------------------------- #
def _matrix_payload():
    """贴近真实形态: 巨量档(30000 手)远高于中位档, 其余档 3000~12000 手。"""
    return _payload([
        _bin(34.62, 9420, 12000, big=200),   # 前2量, 竞买 78.5% → 托举
        _bin(35.80, 1969, 11000, big=0),     # 前3量, 竞买 17.9% → 抛压
        _bin(35.32, 21600, 30000, big=0),    # 巨量 + 无大单 + 竞买 72% → 拆单吸筹
        _bin(34.90, 3060, 6000, big=100),
        _bin(35.10, 2450, 5000, big=100),
        _bin(34.80, 1200, 3000, big=0),
    ])


def test_build_matrix_aggregates_day_and_tags_levels():
    out = build_matrix(_matrix_payload(), symbol="002361", quote={"close": 35.0, "outer_ratio": 0.52})
    assert out["available"] is True
    assert out["symbol"] == "002361"
    assert out["trade_date"] == "2026-10-16"
    assert out["quality"]["bins"] == 6
    assert out["quality"]["identity_ok_ratio"] == 1.0

    day = out["day"]
    assert day["total_vol"] == pytest.approx(67000)
    assert day["buy_vol"] == pytest.approx(39699)
    assert day["sell_vol"] == pytest.approx(27301)
    assert day["net_vol"] == pytest.approx(12398)
    assert day["buy_ratio"] == pytest.approx(39699 / 67000, rel=1e-4)

    tags = {b["price"]: b["tags"] for b in out["bins"]}
    assert "strong_support" in tags[34.62]
    assert "strong_resistance" in tags[35.8]
    assert "iceberg_buy" in tags[35.32]
    assert out["summary"][0]["price"] == pytest.approx(34.62)


def test_build_matrix_marks_cross_check_as_disclosure_only():
    out = build_matrix(_matrix_payload(), symbol="002361", quote={"close": 35.0, "outer_ratio": 0.52})
    cc = out["quality"]["cross_check"]
    assert cc["quote_outer_ratio"] == pytest.approx(0.52)
    assert "不互校" in cc["note"]


def test_build_matrix_degrades_when_bins_insufficient():
    out = build_matrix(_payload([_bin(10.0, 500, 1000)]), symbol="002361")
    assert out["available"] is False
    assert str(MIN_BINS) in out["reason"]
    assert out["bins"] == []
    assert out["support_levels"] == []


def test_build_matrix_degrades_on_empty_payload():
    out = build_matrix(None, symbol="002361")
    assert out["available"] is False
    assert out["reason"]
    assert out["source"] == "tencent_price"


def test_build_matrix_marks_degraded_identity():
    p = _matrix_payload()
    p["identity_ok"], p["identity_bad"] = 4, 2
    out = build_matrix(p, symbol="002361")
    assert out["quality"]["identity_ok_ratio"] == pytest.approx(4 / 6, abs=1e-4)
    assert out["quality"]["degraded"] is True


def test_build_matrix_no_big_column_disables_iceberg():
    rows = [
        _bin(34.62, 7850, 10000, big=0),
        _bin(35.80, 1790, 10000, big=0),
        _bin(35.32, 7200, 10000, big=0),
        _bin(34.90, 5100, 9000, big=0),
        _bin(35.10, 4900, 9000, big=0),
        _bin(34.80, 2000, 4000, big=0),
    ]
    out = build_matrix(_payload(rows), symbol="002361")
    assert out["quality"]["big_data_present"] is False
    assert out["iceberg_signals"] == []
    assert "未检测到" in out["summary"][2]["text"]


# --------------------------------------------------------------------------- #
# 取数编排降级
# --------------------------------------------------------------------------- #
def test_fetch_analysis_degrades_on_vendor_exception():
    def boom(*a, **kw):
        raise RuntimeError("腾讯超时")

    out = fetch_analysis("002361", bins_fn=boom, quote_fn=lambda *a: None)
    assert out["available"] is False
    assert "腾讯超时" in out["reason"]


def test_fetch_analysis_rejects_non_cn_market():
    out = fetch_analysis("00700", market="HK")
    assert out["available"] is False
    assert "仅支持 A 股" in out["reason"]


def test_fetch_analysis_blank_symbol():
    out = fetch_analysis("  ")
    assert out["available"] is False
    assert out["reason"] == "缺少标的代码"


def test_fetch_analysis_passes_vendor_payload_through(monkeypatch):
    captured = {}

    def fake_bins(sym, limit=70):
        captured["limit"] = limit
        return _matrix_payload()

    out = fetch_analysis(
        "002361", limit=42, bins_fn=fake_bins, quote_fn=lambda *a: {"close": 35.0, "name": "神剑股份"}
    )
    assert out["available"] is True
    assert captured["limit"] == 42
    assert out["name"] == "神剑股份"


# --------------------------------------------------------------------------- #
# 集合竞价档(无主动方)不得被读成"砸盘/拆单"
# --------------------------------------------------------------------------- #
def test_auction_bin_is_excluded_from_detection_and_day_ratio():
    """收盘竞价那一档 buy=0(无主动方), 若参与判定会被读成 100% 主动卖的密集抛压。

    实测: 600519@1329.00、601318@51.88 都踩过 —— 必须按"方向不可归因"剔除,
    但成交量仍计入全日(它是真实成交, 不是脏数据)。
    """
    rows = [
        _bin(10.00, 8000, 10000),   # 80% 主动买 → 托举
        _bin(10.10, 5000, 9000),
        _bin(10.20, 4500, 8000),
        _bin(9.90, 1000, 2000),
        _bin(9.80, 500, 1000),
        _bin(10.50, 0, 10000),      # 收盘竞价: 单向 10000 手(占 25%)
    ]
    out = build_matrix(
        _payload(rows), symbol="600519", quote={"close": 10.50, "open": 10.05, "outer_ratio": 0.5}
    )
    auction = next(b for b in out["bins"] if b["price"] == pytest.approx(10.50))
    assert auction["undirected"] is True
    assert "undirected" in auction["tags"]

    # 不被判成抛压/拆单(否则收盘竞价会被当成主力砸盘)
    assert all(lv["price"] != pytest.approx(10.50) for lv in out["resistance_levels"])
    assert all(i["price"] != pytest.approx(10.50) for i in out["iceberg_signals"])

    # 竞买率只按方向可归因的档算: 19000/30000
    assert out["day"]["undirected_vol"] == pytest.approx(10000)
    assert out["day"]["buy_ratio"] == pytest.approx(19000 / 30000, rel=1e-4)
    assert out["day"]["total_vol"] == pytest.approx(40000)


def test_bin_at_auction_price_with_two_sides_is_still_excluded():
    """收盘价那档即便双向都有量, 也混入了 14:57-15:00 集合竞价 → 方向不可分离, 不判定。

    实测反例: 300750@277.00 是收盘价、双向都有量, 却被读出"主动卖占比 93.2%" ——
    竞价量把方向带偏了, 照常判定就会报一个假的密集抛压。
    """
    rows = [
        _bin(10.00, 8000, 10000),
        _bin(10.10, 5000, 9000),
        _bin(10.20, 4500, 8000),
        _bin(9.90, 1000, 2000),
        _bin(9.80, 500, 1000),
        _bin(10.50, 1000, 10000),   # 收盘价上有买有卖(竞价+连续混合)
    ]
    out = build_matrix(_payload(rows), symbol="600519", quote={"close": 10.50})
    row = next(b for b in out["bins"] if b["price"] == pytest.approx(10.50))
    assert row["undirected"] is True
    assert "undirected" in row["tags"]
    assert all(lv["price"] != pytest.approx(10.50) for lv in out["resistance_levels"])


def test_iceberg_requires_economic_size():
    """量比够大但成交额不足 2000 万的小档不算拆单(几千手的冷门档没有实战含义)。"""
    rows = [normalize_bin(_bin(5.0, 4500, 5000), 8000)] + [
        normalize_bin(_bin(4.98 + i * 0.01, 300, 500), 8000) for i in range(5)
    ]
    # 5000 手 × 5 元 × 100 = 250 万元 < 2000 万
    assert detect_iceberg_signals(rows, day_vol=8000, big_data_present=True, day_big_ratio=0.05) == []
