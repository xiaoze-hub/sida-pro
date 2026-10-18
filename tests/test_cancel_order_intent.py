# -*- coding: utf-8 -*-
import pytest
from src.core.cancel_order_intent import baseline_coords, calc_cancel_order_intent


def test_calc_cancel_order_intent_empty_data():
    res = calc_cancel_order_intent(None)
    assert res["available"] is False
    assert res["intent_tag"] == "无数据"

    res_empty = calc_cancel_order_intent({})
    assert res_empty["available"] is False
    assert res_empty["intent_tag"] == "无数据"


def test_calc_cancel_order_intent_zero_cancels():
    res = calc_cancel_order_intent({"BCancel": "0.0", "SCancel": "0.0"})
    assert res["available"] is False
    assert res["intent_tag"] == "无撤单"


def test_calc_cancel_order_intent_shenjian_real_data():
    # 2026-10-16 神剑股份 002361 实测数据
    more_info = {
        "BCancel": "366830.00",
        "SCancel": "516870.00",
        "TotalBVol": "1158850.13",
        "TotalSVol": "1401193.13",
        "L2OrderNum": "174402",
        "L2TicNum": "85998",
    }
    res = calc_cancel_order_intent(more_info, pct_chg=3.78)
    assert res["available"] is True
    assert res["b_cancel"] == 366830.0
    assert res["s_cancel"] == 516870.0
    assert 0.30 <= res["b_cancel_ratio"] <= 0.35
    assert 0.35 <= res["s_cancel_ratio"] <= 0.40
    # 卖盘撤单多于买盘 (51.6万 vs 36.6万)
    assert res["cancel_bias"] < 0
    assert res["intent_tag"] in ["抛压减退", "🛡️ 压盘吸筹", "多空均衡"]


def test_calc_cancel_order_intent_fake_bidding_warning():
    # 模拟高位放量诱多：买盘大单疯狂撤单，卖盘几乎不撤
    more_info = {
        "BCancel": "800000.00",
        "SCancel": "100000.00",
        "TotalBVol": "1500000.00",
        "TotalSVol": "1200000.00",
    }
    res = calc_cancel_order_intent(more_info, pct_chg=4.5)
    assert res["available"] is True
    assert res["cancel_bias"] > 0.5
    assert res["intent_tag"] == "⚠️ 托单诱多"
    assert res["warning_level"] == "warning"
    assert "谨防主力诱多出货" in res["intent_desc"]


def test_calc_cancel_order_intent_fake_selling_accumulation():
    # 模拟低位蓄势吸筹：上方卖单挂了又撤，股价不跌
    more_info = {
        "BCancel": "120000.00",
        "SCancel": "900000.00",
        "TotalBVol": "1000000.00",
        "TotalSVol": "1500000.00",
    }
    res = calc_cancel_order_intent(more_info, pct_chg=-0.3)
    assert res["available"] is True
    assert res["cancel_bias"] < -0.5
    assert res["intent_tag"] == "🛡️ 压盘吸筹"
    assert res["warning_level"] == "caution"
    assert "疑似主力压单恐吓吸筹" in res["intent_desc"]


# ── I-3 动态基线坐标: 固定阈值 → 相对 z 口径 ────────────────────────────

#: 撤买率 42% / 撤卖率 10% / 偏向 +0.62 —— 旧固定阈值(>=40%)必然报"托单诱多"
_FIXED_THRESHOLD_TRAP = {
    "BCancel": "420000.00",
    "SCancel": "100000.00",
    "TotalBVol": "1000000.00",
    "TotalSVol": "1000000.00",
}

#: 该票自身常态: 中位数 40%、标准差 9pp、样本 60 日
_BASELINE = {"median": 0.40, "std": 0.09, "n": 60, "percentile": 59.0}


def test_baseline_z_suppresses_fixed_threshold_false_positive():
    """核心升级: 42% 撤单率在固定 40% 阈值下会误报, 但相对它自己 z=0.22 → 常态."""
    strict = calc_cancel_order_intent(_FIXED_THRESHOLD_TRAP, pct_chg=4.5)
    # 无基线时保持历史行为(固定阈值仍会报) —— 回归护栏
    assert strict["intent_tag"] == "⚠️ 托单诱多"
    assert strict["decision_basis"] == "fixed_threshold"

    res = calc_cancel_order_intent(_FIXED_THRESHOLD_TRAP, pct_chg=4.5, baseline=_BASELINE)
    assert res["decision_basis"] == "baseline_z"
    assert res["baseline_available"] is True
    assert res["baseline_days"] == 60
    assert res["zscore"] == pytest.approx(0.02 / 0.09, abs=1e-3)
    assert res["intent_tag"] == "常态"
    assert res["warning_level"] == "safe"
    assert "固定 40% 阈值口径在此不适用" in res["intent_desc"]
    assert "z=+0.22" in res["intent_desc"]


def test_baseline_z_flags_true_relative_fake_bidding():
    res = calc_cancel_order_intent(
        {"BCancel": "620000.00", "SCancel": "100000.00",
         "TotalBVol": "1000000.00", "TotalSVol": "1000000.00"},
        pct_chg=4.2,
        baseline=_BASELINE,
    )
    assert res["zscore"] == pytest.approx(2.4444, abs=1e-3)
    assert res["intent_tag"] == "⚠️ 相对异常诱多"
    assert res["warning_level"] == "warning"
    assert "为近 60 日 P59" in res["intent_desc"]


def test_baseline_elevated_without_rally_only_observes():
    res = calc_cancel_order_intent(
        {"BCancel": "620000.00", "SCancel": "100000.00",
         "TotalBVol": "1000000.00", "TotalSVol": "1000000.00"},
        pct_chg=1.0,
        baseline=_BASELINE,
    )
    assert res["intent_tag"] == "撤单抬升·涨幅未配合"
    assert res["warning_level"] == "caution"


def test_baseline_shrinking_cancel_with_breakdown_is_real_selling():
    res = calc_cancel_order_intent(
        {"BCancel": "200000.00", "SCancel": "550000.00",
         "TotalBVol": "1000000.00", "TotalSVol": "1000000.00"},
        pct_chg=-3.4,
        baseline=_BASELINE,
    )
    assert res["zscore"] < -1.0
    assert res["intent_tag"] == "抛压真实确认"
    assert res["warning_level"] == "caution"


def test_baseline_with_too_few_days_falls_back_to_fixed_threshold():
    res = calc_cancel_order_intent(
        _FIXED_THRESHOLD_TRAP, pct_chg=4.5,
        baseline={"median": 0.40, "std": 0.09, "n": 5},
    )
    assert res["baseline_available"] is False
    assert res["zscore"] is None
    assert res["decision_basis"] == "fixed_threshold"
    assert res["intent_tag"] == "⚠️ 托单诱多"


def test_baseline_zero_dispersion_falls_back_to_fixed_threshold():
    res = calc_cancel_order_intent(
        _FIXED_THRESHOLD_TRAP, pct_chg=4.5,
        baseline={"median": 0.40, "std": 0.0, "n": 60},
    )
    assert res["zscore"] is None
    assert res["decision_basis"] == "fixed_threshold"


def test_baseline_flat_alias_keys_are_accepted():
    """profile.buy 的扁平别名(b_median/b_std/b_n)也必须能喂进来。"""
    res = calc_cancel_order_intent(
        _FIXED_THRESHOLD_TRAP, pct_chg=4.5,
        baseline={"b_median": "0.40", "b_std": "0.09", "b_n": "60", "b_percentile": "59"},
    )
    assert res["baseline_available"] is True
    assert res["baseline_median"] == pytest.approx(0.40)
    assert res["baseline_days"] == 60
    assert res["decision_basis"] == "baseline_z"


def test_baseline_coords_rejects_garbage():
    assert baseline_coords(0.42, None)["available"] is False
    assert baseline_coords(None, _BASELINE)["z"] is None
    assert baseline_coords(0.42, {"median": "x", "std": "y", "n": "z"})["available"] is False


def test_no_more_info_branch_keeps_schema_with_baseline():
    res = calc_cancel_order_intent(None, baseline=_BASELINE)
    for key in ("decision_basis", "baseline_available", "baseline_median",
                "baseline_std", "baseline_days", "zscore", "percentile"):
        assert key in res
    assert res["available"] is False
