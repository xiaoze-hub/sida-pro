"""持仓成本锚定（2026-10-23）：四个区域各一例、无成本不假设、数字不许漂、入参不许改。"""
import copy

from src.core.scenario_cost_anchor import enrich_with_cost, render_cost_anchor

NO_COST = "[成本锚定] 无持仓成本——不假设你的成本。"


def _matrix(price: float, support: float, resist: float) -> dict:
    """与 scenario_engine.build_matrix 同形状（关键位都已是 2 位小数）。"""
    return {
        "available": True,
        "levels": {
            "price": price,
            "resist": resist,
            "support": support,
            "ma20": resist,
            "ma60": support,
            "band_hi": None,
            "band_lo": None,
            "vwap": None,
        },
        "context": {"position": 55.0, "rs20": None, "momentum_20": None, "vol_ratio": 1.1},
        "scenarios": [{"name": "走强", "trigger": "t", "target": resist,
                       "response": "r", "invalid": "i"}],
        "disclaimer": "不预测方向。",
        "price": price,
        "as_of": "2026-10-23",
    }


def _anchor(price, support, resist, cost, stop=None):
    return enrich_with_cost(_matrix(price, support, resist),
                            cost_price=cost, stop_price=stop)["cost_anchor"]


def test_zone_trapped():
    a = _anchor(30.0, 28.0, 33.0, 35.0)
    assert a["available"] is True
    assert a["zone"] == "套牢区"


def test_zone_in_cost_range():
    a = _anchor(30.5, 29.0, 32.0, 31.0)
    assert a["zone"] == "成本区间"


def test_zone_cushion():
    a = _anchor(34.0, 30.0, 36.0, 31.0)
    assert a["zone"] == "安全垫区"
    assert a["distance_to_cost_pct"] == 9.68        # (34-31)/31


def test_zone_broken():
    a = _anchor(28.0, 29.0, 33.0, 31.0, stop=29.0)
    assert a["zone"] == "破位区"
    assert a["distance_to_support_pct"] == -3.45    # 现价在支撑下方
    assert a["distance_to_cost_pct"] == -9.68


def test_breakdown_beats_trapped():
    """跌破支撑比套牢更急：现价 < 支撑 且 成本 > 阻力，仍判破位区。"""
    assert _anchor(27.0, 29.0, 33.0, 35.0)["zone"] == "破位区"


def test_no_cost_is_explicit_and_matrix_untouched():
    m = _matrix(30.5, 29.0, 32.0)
    snapshot = copy.deepcopy(m)
    out = enrich_with_cost(m, cost_price=None)

    assert out is not m
    assert m == snapshot                            # 入参一个字节都没动
    assert out["cost_anchor"] == {
        "available": False,
        "reason": "无持仓成本（未上传交割单或无该股持仓）",
    }
    for key, val in snapshot.items():                # 原有键原样保留
        assert out[key] == val
    assert render_cost_anchor(out["cost_anchor"]) == NO_COST


def test_zero_or_negative_cost_is_no_cost():
    for bad in (0, -1.0, "abc", None):
        assert _anchor(30.5, 29.0, 32.0, bad)["available"] is False


def test_no_stop_price_no_discipline_level():
    """没设纪律位就不许出现纪律位（绝不默认 -5%）。"""
    a = _anchor(30.5, 29.0, 32.0, 31.0, stop=None)
    labels = [lv["label"] for lv in a["levels_for_owner"]]
    assert labels == ["成本", "支撑", "阻力"]
    assert a["stop_price"] is None
    assert "纪律止损" not in render_cost_anchor(a)
    assert "-5%" not in render_cost_anchor(a)


def test_numbers_come_from_matrix_exactly():
    a = _anchor(34.0, 30.0, 36.0, 31.0, stop=29.5)
    by_label = {lv["label"]: lv["price"] for lv in a["levels_for_owner"]}
    assert by_label["支撑"] == 30.0
    assert by_label["阻力"] == 36.0
    assert by_label["纪律止损"] == 29.5
    assert by_label["成本"] == 31.0
    assert a["cost_price"] == 31.0
    assert a["break_even_price"] == 31.0
    assert a["price"] == 34.0
    assert a["distance_to_cost_pct"] == round((34.0 - 31.0) / 31.0 * 100, 2)


def test_levels_for_owner_speaks_position_language():
    a = _anchor(30.0, 28.0, 33.0, 35.0, stop=34.0)
    text = {lv["label"]: lv["meaning"] for lv in a["levels_for_owner"]}
    assert "浮亏" in text["成本"] or "距成本" in text["成本"]
    assert "33.00" in text["阻力"] and "浮亏" in text["阻力"]     # 阻力仍在成本下方
    assert "34.00" in text["纪律止损"]
    assert "-2.86%" in text["纪律止损"]                          # (34-35)/35


def test_render_is_short_and_not_advice():
    out = render_cost_anchor(_anchor(34.0, 30.0, 36.0, 31.0, stop=29.5))
    lines = out.splitlines()
    assert 3 <= len(lines) <= 6
    assert "非买卖建议" in out
    assert "安全垫区" in out


def test_missing_levels_refuses_to_anchor():
    m = _matrix(30.0, 28.0, 33.0)
    m["levels"]["support"] = None
    a = enrich_with_cost(m, cost_price=31.0)["cost_anchor"]
    assert a["available"] is False
    assert "缺关键位" in a["reason"]
