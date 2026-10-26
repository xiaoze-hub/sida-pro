# -*- coding: utf-8 -*-
"""I-3 委托行为画像(撤单率历史基线 + 动态 Z-Score)单元测试.

锁住四类不变量:
① **口径**: 撤单率 = BCancel/BOrder, 缺值 → None（0 不能顶替缺失）, 有界 [0,1];
② **统计**: 中位数/标准差/分位/z 逐值对拍（手算期望, 不用被测函数自证）;
③ **判定**: z>2 且涨幅>3% 才算相对异常诱多; |z|<1 一律常态（打破 40% 固定阈值误报）;
④ **降级诚实**: 基线 <20 日 / 方差为 0 / 网关异常 → 不给相对判定, 不编造数字。
"""
from __future__ import annotations

import statistics

import pytest

from src.core.cancel_profile import (
    MIN_BASELINE_DAYS,
    build_cancel_profile,
    cancel_rate,
    describe,
    fetch_cancel_profile,
    norm_date,
    normalize_exday_row,
    percentile_rank,
    zscore,
)


def _row(date: str, b_rate: float, *, s_rate: float = 0.35, order: float = 100000.0) -> dict:
    """造一行 TQ 日线统计: 买撤率/卖撤率可精确控制(order 固定, 撤单量 = rate×order)。"""
    return {
        "Date": date.replace("-", ""),
        "BCancel": b_rate * order,
        "BOrder": order,
        "SCancel": s_rate * order,
        "SOrder": order,
        "CJBS": 1000,
    }


def _dates(n: int, *, start_day: int = 1) -> list[str]:
    """生成 n 个连续自然日的 ISO 日期(避开 2 月等边界, 统一用 2026 年内)。"""
    from datetime import date, timedelta

    base = date(2026, 7, 1) + timedelta(days=start_day - 1)
    return [(base + timedelta(days=i)).isoformat() for i in range(n)]


def _steady_rows(baseline_n: int = 60, *, today_rate: float | None = 0.62) -> list[dict]:
    """基线交替 0.30/0.50（中位数 0.40, 样本 std=0.10084）+ 可选的今日行。"""
    dates = _dates(baseline_n + (1 if today_rate is not None else 0))
    rows = [_row(d, 0.30 if i % 2 == 0 else 0.50) for i, d in enumerate(dates[:baseline_n])]
    if today_rate is not None:
        rows.append(_row(dates[baseline_n], today_rate))
    return rows


# ── ① 口径 ────────────────────────────────────────────────────────────

def test_cancel_rate_primary_and_fallback_caliber():
    # 委托量含已撤: rate = cancel/order
    assert cancel_rate(30000.0, 100000.0) == pytest.approx(0.30)
    # 委托量是剩余量(小于撤单量): 回退到 cancel/(cancel+order)
    assert cancel_rate(80000.0, 20000.0) == pytest.approx(0.8)
    # 边界
    assert cancel_rate(120.0, 100.0) == pytest.approx(120 / 220)
    assert cancel_rate(0.0, 100.0) == 0.0
    assert cancel_rate(100.0, 100.0) == 1.0


@pytest.mark.parametrize(
    "cancel,order",
    [(None, 100.0), (100.0, None), (100.0, 0.0), (100.0, -1.0), (-1.0, 100.0), ("x", 100.0)],
)
def test_cancel_rate_missing_or_invalid_is_none_never_zero(cancel, order):
    """缺失/非法 → None。0 是真实值, 不能拿来顶替缺失(否则"没数据"会被读成"撤单率为0")。"""
    assert cancel_rate(cancel, order) is None


def test_norm_date_accepts_compact_and_iso():
    assert norm_date("20261017") == "2026-10-17"
    assert norm_date("2026-10-17") == "2026-10-17"
    assert norm_date("2026/10/17") == "2026-10-17"
    assert norm_date("") == ""
    assert norm_date(None) == ""


def test_normalize_exday_row_computes_rates_and_keeps_raw_volumes():
    row = normalize_exday_row({"Date": "20261017", "BCancel": "366830", "BOrder": "1158850",
                               "SCancel": "516870", "SOrder": "1401193"})
    assert row["date"] == "2026-10-17"
    assert row["b_rate"] == pytest.approx(366830 / 1158850)
    assert row["s_rate"] == pytest.approx(516870 / 1401193)
    assert row["b_cancel"] == 366830.0


def test_normalize_accepts_already_lowercased_keys():
    row = normalize_exday_row({"date": "2026-10-17", "b_cancel": 300.0, "b_order": 1000.0})
    assert row["b_rate"] == pytest.approx(0.3)


# ── ② 统计 ────────────────────────────────────────────────────────────

def test_describe_matches_hand_computed_median_and_sample_std():
    vals = [0.30, 0.50] * 30  # n=60
    d = describe(vals)
    assert d["n"] == 60
    assert d["median"] == pytest.approx(0.40)
    # 样本标准差(ddof=1): sqrt(0.01 * 60/59)
    assert d["std"] == pytest.approx((0.01 * 60 / 59) ** 0.5, rel=1e-12)
    assert d["min"] == 0.30 and d["max"] == 0.50
    assert d["p10"] == pytest.approx(0.30)
    assert d["p90"] == pytest.approx(0.50)


def test_describe_single_sample_has_no_std():
    d = describe([0.4])
    assert d["n"] == 1 and d["std"] is None


def test_zscore_is_none_without_meaningful_dispersion():
    assert zscore(0.6, 0.4, 0.1) == pytest.approx(2.0)
    assert zscore(0.6, 0.4, 0.0) is None      # 方差为 0 → 不给伪 z
    assert zscore(0.6, 0.4, None) is None
    assert zscore(None, 0.4, 0.1) is None


def test_percentile_rank_is_ecdf():
    vals = [0.1, 0.2, 0.3, 0.4]
    assert percentile_rank(0.4, vals) == 100.0
    assert percentile_rank(0.2, vals) == 50.0
    assert percentile_rank(0.05, vals) == 0.0
    assert percentile_rank(None, vals) is None


# ── ③ 判定 ────────────────────────────────────────────────────────────

def test_normal_day_is_not_flagged_against_fixed_threshold():
    """核心诉求: 撤单率 42%(>40% 固定阈值) 但相对自身常态 |z|<1 → 常态, 不报警."""
    profile = build_cancel_profile(_steady_rows(today_rate=0.42), pct_chg=2.0)
    assert profile["available"] is True
    assert profile["baseline_days"] == 60
    assert profile["buy"]["median"] == pytest.approx(0.40)
    assert abs(profile["buy"]["z"]) < 1.0
    assert profile["verdict"]["code"] == "normal"
    assert profile["verdict"]["level"] == "safe"
    assert "常态" in profile["conclusion"]
    assert "40%" not in profile["conclusion"] or "不适用" in profile["conclusion"]


def test_z_above_two_with_rally_is_relative_fake_bidding():
    profile = build_cancel_profile(_steady_rows(today_rate=0.62), pct_chg=4.2)
    z = profile["buy"]["z"]
    assert z == pytest.approx((0.62 - 0.40) / ((0.01 * 60 / 59) ** 0.5), rel=1e-9)
    assert z > 2.0
    assert profile["buy"]["percentile"] == 100.0
    assert profile["verdict"]["code"] == "fake_bidding"
    assert profile["verdict"]["level"] == "warning"
    assert profile["verdict"]["abnormal"] is True
    # 一句话结论必须同时给出 今日值 / 分位 / 常态区间 / z
    assert "今日买撤 62.0%" in profile["conclusion"]
    assert "P100" in profile["conclusion"]
    assert "z=+2.18" in profile["conclusion"]


def test_z_above_two_without_rally_is_only_observation():
    profile = build_cancel_profile(_steady_rows(today_rate=0.62), pct_chg=1.0)
    assert profile["verdict"]["code"] == "elevated_no_rally"
    assert profile["verdict"]["level"] == "caution"


def test_shrinking_cancel_with_breakdown_confirms_real_selling():
    profile = build_cancel_profile(_steady_rows(today_rate=0.20), pct_chg=-3.0)
    assert profile["buy"]["z"] < -1.0
    assert profile["verdict"]["code"] == "real_selling"
    assert profile["verdict"]["level"] == "caution"


def test_low_z_without_dropdown_is_not_bearish():
    profile = build_cancel_profile(_steady_rows(today_rate=0.20), pct_chg=0.5)
    assert profile["verdict"]["code"] == "quiet"
    assert profile["verdict"]["level"] == "safe"


def test_moderate_deviation_between_one_and_two_sigma_is_mild():
    # 0.50 → z ≈ 0.99? 取 0.51 保证 1<z<2
    profile = build_cancel_profile(_steady_rows(today_rate=0.51), pct_chg=0.0)
    z = profile["buy"]["z"]
    assert 1.0 <= z < 2.0
    assert profile["verdict"]["code"] == "mild"


# ── ④ 降级诚实 ────────────────────────────────────────────────────────

def test_short_baseline_gives_no_relative_verdict():
    profile = build_cancel_profile(_steady_rows(baseline_n=5, today_rate=0.62), pct_chg=4.0)
    assert profile["available"] is True          # 今日值是真的, 照给
    assert profile["baseline_days"] == 5
    assert profile["buy"]["z"] is None
    assert profile["verdict"]["code"] == "insufficient"
    assert "暂不给相对判定" in profile["conclusion"]
    assert "需 ≥20 日" in profile["conclusion"]


def test_zero_variance_baseline_does_not_produce_z():
    rows = [_row(d, 0.40) for d in _dates(40)] + [_row(_dates(41)[-1], 0.90)]
    profile = build_cancel_profile(rows, pct_chg=5.0)
    assert profile["baseline_days"] == 40
    assert profile["buy"]["std"] == 0.0
    assert profile["buy"]["z"] is None
    assert profile["verdict"]["code"] == "insufficient"


def test_missing_order_volume_degrades_to_unavailable_not_zero():
    rows = [_row(d, 0.4) for d in _dates(30)]
    rows.append({"Date": "20260801", "BCancel": 30000.0, "BOrder": None})  # 今日缺委托量
    profile = build_cancel_profile(rows, pct_chg=1.0)
    assert profile["available"] is False
    assert "缺失" in profile["reason"]
    assert profile["today"]["b_rate"] is None
    assert profile["series"]  # 序列照给, 缺失处为 None


def test_empty_and_garbage_inputs_are_unavailable():
    for rows in ([], None, [{"Date": ""}], ["not-a-dict"]):
        profile = build_cancel_profile(rows)  # type: ignore[arg-type]
        assert profile["available"] is False
        assert profile["series"] == []
        assert profile["buy"]["rate"] is None


def test_duplicate_date_keeps_last_and_series_is_sorted():
    rows = [_row("2026-07-02", 0.30), _row("2026-07-01", 0.20), _row("2026-07-02", 0.50)]
    profile = build_cancel_profile(rows)
    assert [r["date"] for r in profile["series"]] == ["2026-07-01", "2026-07-02"]
    assert profile["series"][-1]["b_rate"] == pytest.approx(0.50)


def test_window_slices_to_requested_length_plus_today():
    rows = [_row(d, 0.30 if i % 2 else 0.50) for i, d in enumerate(_dates(200))]
    profile = build_cancel_profile(rows, window=60)
    assert len(profile["series"]) == 61          # 60 个基线日 + 今日
    assert profile["baseline_days"] == 60
    assert profile["window"] == 60


def test_today_is_last_row_not_wall_clock():
    """今日 = 最后一行(非交易日就是最近交易日), 不得用 now() 假造当日。"""
    rows = [_row(d, 0.4) for d in _dates(30)]
    profile = build_cancel_profile(rows)
    assert profile["as_of"] == _dates(30)[-1]
    assert profile["today"]["date"] == _dates(30)[-1]


def test_default_min_baseline_is_documented_constant():
    assert MIN_BASELINE_DAYS == 20


# ── 取数层(注入, 不触网) ─────────────────────────────────────────────

def test_fetch_passes_window_plus_one_to_vendor():
    seen: dict = {}

    def fake(code: str, count: int):
        seen["code"] = code
        seen["count"] = count
        return _steady_rows(today_rate=0.62)

    profile = fetch_cancel_profile("002361", days=60, pct_chg=4.0, exday_fn=fake)
    assert seen == {"code": "002361", "count": 61}
    assert profile["available"] is True
    assert profile["symbol"] == "002361"


def test_fetch_rejects_non_cn_market_without_calling_vendor():
    called = []

    def fake(code: str, count: int):
        called.append(code)
        return []

    profile = fetch_cancel_profile("00700", market="HK", exday_fn=fake)
    assert called == []
    assert profile["available"] is False
    assert "仅支持 A 股" in profile["reason"]


def test_fetch_degrades_when_vendor_raises():
    def boom(code: str, count: int):
        raise RuntimeError("TQ 未连接")

    profile = fetch_cancel_profile("002361", exday_fn=boom)
    assert profile["available"] is False
    assert "TQ 未连接" in profile["reason"]
    assert profile["series"] == []


def test_fetch_blank_symbol_is_unavailable():
    profile = fetch_cancel_profile("  ")
    assert profile["available"] is False
    assert profile["reason"]
