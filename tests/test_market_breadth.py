"""市场广度指标算法测试 —— 手算校核 + 边界。"""

import pytest

try:
    from src.core.market_breadth import (
        BreadthBar,
        compute_series,
        latest_summary,
        percentile_rank,
        sentiment_score,
    )
except ImportError:  # pragma: no cover
    from core.market_breadth import (  # type: ignore
        BreadthBar,
        compute_series,
        latest_summary,
        percentile_rank,
        sentiment_score,
    )


def _bars(n_win=12):
    out = []
    for i in range(n_win):
        out.append(
            BreadthBar(
                trade_date=f"2026-09-{i + 1:02d}",
                up=60 + i,
                down=40,
                flat=5,
                up_volume=6000.0 + i,
                down_volume=4000.0,
            )
        )
    return out


def test_adl_is_cumulative_up_minus_down():
    bars = _bars()
    rows = compute_series(bars)
    assert rows[0]["adl"] == 20.0
    assert rows[1]["adl"] == 41.0
    assert rows[-1]["adl"] == sum(b.up - b.down for b in bars)


def test_adr_hand_computed_last_day():
    bars = _bars()
    rows = compute_series(bars)
    exp = sum(b.up for b in bars[-10:]) / 10 / (sum(b.down for b in bars[-10:]) / 10)
    assert rows[-1]["adr"] == pytest.approx(exp, abs=1e-4)
    assert rows[0]["adr"] is None  # 窗口不足 -> None，不猜


def test_arms_is_ad_ratio_over_volume_ratio():
    bars = _bars()
    rows = compute_series(bars)
    b = bars[-1]
    exp = (b.up / b.down) / (b.up_volume / b.down_volume)
    assert rows[-1]["arms"] == pytest.approx(exp, abs=1e-4)


def test_arms_none_when_down_volume_zero():
    bars = [BreadthBar("2026-09-01", up=10, down=5, up_volume=100.0, down_volume=0.0)]
    rows = compute_series(bars)
    assert rows[0]["arms"] is None


def test_bti_and_stix_match_definition():
    bars = _bars()
    rows = compute_series(bars)
    pcts = [b.up * 100.0 / (b.up + b.down) for b in bars]
    assert rows[-1]["bti"] == pytest.approx(sum(pcts[-10:]) / 10, abs=1e-2)
    assert rows[-1]["stix"] is not None


def test_mcl_first_value_is_zero_because_emas_start_equal():
    rows = compute_series(_bars())
    assert rows[0]["mcl"] == pytest.approx(0.0, abs=1e-9)
    assert rows[-1]["mcl_summation"] is not None


def test_missing_volume_gives_none_not_zero():
    bars = [BreadthBar("2026-09-01", up=10, down=5)]
    rows = compute_series(bars)
    assert rows[0]["arms"] is None and rows[0]["up_volume"] == 0.0


def test_percentile_rank_edges():
    series = [float(i) for i in range(20)]
    assert percentile_rank(series, 0.0) == pytest.approx(2.5, abs=0.1)
    assert percentile_rank(series, 19.0) == pytest.approx(97.5, abs=0.1)
    assert percentile_rank(series, 100.0) == 100.0
    assert percentile_rank([1.0, 2.0], 1.0) is None  # 样本不足 20


def test_sentiment_score_ignores_missing_and_returns_none_when_all_missing():
    assert sentiment_score({"adl_pct": None, "adr_pct": None, "arms_pct": None,
                            "bti_pct": None, "mcl_pct": None, "stix_pct": None}) is None
    assert sentiment_score({"adl_pct": 50.0, "adr_pct": 70.0, "arms_pct": None,
                            "bti_pct": None, "mcl_pct": None, "stix_pct": None}) == 60


def test_latest_summary_shape_and_caliber_label():
    out = latest_summary(_bars(30))
    assert out["bars_used"] == 30
    assert "自研口径" in out["caliber"]
    assert out["trade_date"] == "2026-09-30"


def test_empty_input_is_honest():
    assert latest_summary([]) == {"ok": False, "reason": "no_data"}
