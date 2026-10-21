"""情景规则引擎（2026-10-21）：不预测方向，只给条件-反应；缺关键位就不硬编。"""
from src.core.scenario_engine import build_scenarios, render

TREND = {
    "available": True,
    "last": 33.0,
    "mas": {5: 31.7, 10: 32.7, 20: 33.9, 60: 35.9},
    "high": 40.0, "low": 30.0, "position": 30.0,
    "vol_ratio": 1.35, "rs20": -3.1,
}


def test_three_scenarios_always_present():
    m = build_scenarios(TREND, {"band70_lo": 32.5, "band70_hi": 44.5})
    assert m["available"] is True
    assert [s["name"] for s in m["scenarios"]] == ["走强", "震荡", "走弱"]
    for s in m["scenarios"]:
        assert s["trigger"] and s["target"] and s["response"] and s["invalid"]


def test_levels_derived_not_invented():
    m = build_scenarios(TREND, {"band70_lo": 32.5, "band70_hi": 44.5})
    lv = m["levels"]
    # 阻力取 MA20(33.9)/筹码带上沿(44.5)/区间高(40) 的最近值 → 33.9
    assert lv["resist"] == 33.9
    # 支撑必须在现价之下 → MA60(35.9) 在上方不算支撑，取筹码带下沿 32.5
    assert lv["support"] == 32.5
    assert lv["support"] < 33.0 < lv["resist"], "支撑/阻力不能倒置"


def test_missing_levels_refuses():
    m = build_scenarios({"available": True, "mas": {}, "high": None, "low": None}, {})
    assert m["available"] is False
    assert "不硬编" in render(m)


def test_render_states_no_direction_prediction():
    m = build_scenarios(TREND, {"band70_lo": 32.5, "band70_hi": 44.5})
    assert "不预测方向" in render(m)
