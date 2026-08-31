# -*- coding: utf-8 -*-
"""阶段1 五件套 ①④⑤ 单测: gs_strategy / ai_activity / resonance。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import ai_activity, gs_strategy, resonance  # noqa: E402


def _mk_bars(n=40, trend=0.1, final_surge=False, final_crash=False):
    bars = []
    price = 20.0
    for i in range(n):
        o = price
        price += trend
        c = price
        bars.append({"date": f"2026-07-{i+1:02d}", "open": o,
                     "high": max(o, c) + 0.05, "low": min(o, c) - 0.05,
                     "close": c, "volume": 100000})
    if final_surge or final_crash:
        o = bars[-1]["close"]
        c = o * (1.20 if final_surge else 0.85)
        bars.append({"date": "2026-08-31", "open": o,
                     "high": max(o, c) + 0.05, "low": min(o, c) - 0.05,
                     "close": c, "volume": 500000})
    return bars


# ---------------------------------------------------------------------------
# ① gs_strategy
# ---------------------------------------------------------------------------


def test_gs_insufficient_bars_marks_no_data():
    r = gs_strategy.eval_gs(_mk_bars(n=10))
    assert r["zone"] is None
    assert "无数据" in r["note"]


def test_gs_last_bar_cross_is_pending_not_confirmed():
    """末根暴力拉升产生的交叉 = 待确认(pending), 不作为已确认 G 输出。"""
    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    r = gs_strategy.eval_gs(bars)
    assert r["signal"] == "G"
    assert r["pending"] is True
    assert r["signal_confirmed"] is False
    assert r["new_g_today"] is False   # 收盘未定死, 不算今日新出 G


def test_gs_historical_cross_is_confirmed():
    """历史交叉(不在末根) = 已确认。"""
    bars = _mk_bars(n=40, trend=-0.05, final_surge=True) + _mk_bars(n=5, trend=0.05)
    r = gs_strategy.eval_gs(bars)
    assert r["signal_confirmed"] is True
    assert r["pending"] is False


def test_gs_zone_follows_state():
    bars = _mk_bars(n=40, trend=0.15)
    r = gs_strategy.eval_gs(bars)
    assert r["zone"] in ("G区", "S区")


def test_trend_label_mapping():
    assert gs_strategy.trend_label({"zone": None}) == "无数据"
    assert gs_strategy.trend_label({"zone": "G区", "signal": "G", "signal_confirmed": True}) == "G信号"
    assert gs_strategy.trend_label({"zone": "G区", "signal": "G", "signal_confirmed": False}) == "G区间"
    assert gs_strategy.trend_label({"zone": "S区", "signal": "S", "signal_confirmed": True}) == "S信号"
    assert gs_strategy.trend_label({"zone": "S区", "signal": "S", "signal_confirmed": False}) == "S区间"


# ---------------------------------------------------------------------------
# ④ ai_activity
# ---------------------------------------------------------------------------


def test_activity_insufficient_marks_no_data():
    r = ai_activity.eval_activity([{"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
    assert r["activity"] is None
    assert "无数据" in r["note"]


def test_activity_thresholds():
    assert ai_activity.activity_of_value(7.0)["level"] == "大牛"
    assert ai_activity.activity_of_value(4.0)["level"] == "强势"
    assert ai_activity.activity_of_value(2.0)["level"] == "生命"
    assert ai_activity.activity_of_value(1.0)["level"] == "弱"
    assert ai_activity.activity_of_value(3.0)["above_strong"] is True
    assert ai_activity.activity_of_value(2.9)["above_strong"] is False
    assert ai_activity.activity_of_value(None)["level"] is None


def test_activity_real_bars():
    bars = _mk_bars(n=10, trend=0.5)
    r = ai_activity.eval_activity(bars)
    assert isinstance(r["activity"], (int, float))
    assert r["level"] in ("弱", "生命", "强势", "大牛")


# ---------------------------------------------------------------------------
# ⑤ resonance 7 行状态表
# ---------------------------------------------------------------------------


def test_resonance_row1_first_resonance():
    r = resonance.evaluate_state("G信号", 4.0, 3.0, 1_000_000.0, 500_000.0)
    assert r["row"] == 1 and r["phase"] == "向好" and r["state"] == "首次共振"
    assert r["backtest"]["win_rate"] == 0.7542


def test_resonance_row2_double_again():
    # 活跃度和资金都较前日翻倍 → 再次共振(拐点)
    r = resonance.evaluate_state("G区间", 8.0, 3.5, 2_000_000.0, 900_000.0)
    assert r["row"] == 2 and r["phase"] == "拐点"


def test_resonance_row3_stable():
    # 强势线上 + 流入, 但未翻倍 → 平稳
    r = resonance.evaluate_state("G区间", 4.0, 3.5, 1_000_000.0, 900_000.0)
    assert r["row"] == 3 and r["phase"] == "向好"


def test_resonance_row4_fund_outflow():
    r = resonance.evaluate_state("G区间", 4.0, 3.5, -500_000.0, 100_000.0)
    assert r["row"] == 4 and r["phase"] == "分歧" and r["bad_count"] == 1


def test_resonance_row5_activity_below():
    r = resonance.evaluate_state("G区间", 2.0, 3.5, 500_000.0, 100_000.0)
    assert r["row"] == 5 and r["phase"] == "分歧" and r["bad_count"] == 1


def test_resonance_row6_two_bad():
    r = resonance.evaluate_state("G区间", 2.0, 3.5, -500_000.0, 100_000.0)
    assert r["row"] == 6 and r["phase"] == "分歧" and r["bad_count"] == 2


def test_resonance_row7_all_bad():
    r = resonance.evaluate_state("S信号", 2.0, 3.5, -500_000.0, 100_000.0)
    assert r["row"] == 7 and r["phase"] == "走坏" and r["bad_count"] == 3
    assert r["backtest"]["win_rate"] == 0.6740


def test_resonance_missing_inputs_marks_no_data():
    r = resonance.evaluate_state("G区间", None, 3.0, 100.0, 50.0)
    assert r["row"] == 0 and r["state"] == "无数据" and "活跃度" in r["note"]
    r2 = resonance.evaluate_state("无数据", 4.0, 3.0, 100.0, 50.0)
    assert r2["state"] == "无数据" and "趋势" in r2["note"]


def test_resonance_double_needs_prev():
    # 缺前值 → 不算翻倍(不猜), 归"平稳"而非"再次共振"
    r = resonance.evaluate_state("G区间", 8.0, None, 2_000_000.0, None)
    assert r["row"] == 3


def test_fund_flow_label():
    assert resonance.fund_flow_label(100.0, 50.0)["direction"] == "流入"
    assert resonance.fund_flow_label(100.0, 50.0)["color"] == "red"
    assert resonance.fund_flow_label(-100.0, 50.0)["direction"] == "流出"
    assert resonance.fund_flow_label(-100.0, 50.0)["color"] == "green"
    assert resonance.fund_flow_label(None, 50.0)["direction"] == "无数据"
    assert resonance.fund_flow_label(100.0, None)["net"] is None
