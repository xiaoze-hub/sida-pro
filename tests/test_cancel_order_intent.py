# -*- coding: utf-8 -*-
import pytest
from src.core.cancel_order_intent import calc_cancel_order_intent


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
