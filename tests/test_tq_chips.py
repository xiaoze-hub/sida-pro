"""个股筹码体检测试（stub 公式引擎，不连网关）。"""

import pytest

try:
    from src.core import tq_chips as C
except ImportError:  # pragma: no cover
    from core import tq_chips as C  # type: ignore


def _fake_raw(**over):
    raw = {
        "_feed": {"bars": 250, "last_date": "2026-09-24", "ErrorId": "0"},
        "SSRP": {"MA1": ["10.56"], "MA2": ["11.14"], "SSRP": ["10.46"]},
        "MCST": {"MCST": ["10.45"]},
        "CYC": {"CYC1": ["10.22"], "CYC2": ["10.39"], "CYC3": ["11.00"], "CYC∞": ["10.45"]},
        "AMV": {"AMV1": ["14.48"], "AMV2": ["10.22"]},
        "PAV": {"CV": ["-86.47"], "DIFF": ["-38.98"], "GV": ["13.53"], "MCV2": ["-67"], "MGV1": ["1"]},
        "PAVE": {"CV": ["-77.37"], "DIFF": ["-38.98"], "MCV": ["-67.07"]},
        "CYW": {"CYW": ["-57.62"]},
        "ZJTJ": {"OUTPUT1": ["1.00"], "主力出货": ["0.00"]},
    }
    raw.update(over)
    return raw


def test_normalize_code():
    assert C.normalize_code("002361") == "002361.SZ"
    assert C.normalize_code("600519") == "600519.SH"
    assert C.normalize_code("920196") == "920196.SH".replace("SH", "BJ")
    assert C.normalize_code("002361.SZ") == "002361.SZ"
    assert C.normalize_code("") == ""
    assert C.normalize_code("abc") == "ABC"


def test_build_chips_extracts_latest_values(monkeypatch):
    import marketdata.vendors.tq as T  # noqa: N812

    monkeypatch.setattr(T, "formula_zb_many", lambda names, code, count=250: _fake_raw())
    out = C.build_chips("002361.SZ", quote={"close": 10.14})
    assert out["ok"] is True
    assert out["trade_date"] == "2026-09-24" and out["bars"] == 250
    assert out["cost"]["chip_peak_cost"] == 10.46
    assert out["cost"]["market_cost"] == 10.45
    assert out["cost"]["cyc"]["CYC1"] == 10.22
    assert out["main_force"]["cyw"] == -57.62
    assert out["main_force"]["zjtj"]["OUTPUT1"] == 1.0
    # 现价 vs 成本线 = 我方计算, 单独放 vs_close
    assert out["vs_close"]["diff_pct"]["chip_peak_cost"]["pct"] == pytest.approx(-3.06, abs=0.01)
    assert out["caliber"].startswith("数据源=通达信客户端指标公式")
    assert "SCR" in out["excluded"]


def test_partial_failure_is_reported_not_faked(monkeypatch):
    import marketdata.vendors.tq as T  # noqa: N812

    raw = _fake_raw()
    raw["CYW"] = {"_error": "TQ formula_zb ErrorId=9: no find formula setting"}
    monkeypatch.setattr(T, "formula_zb_many", lambda names, code, count=250: raw)
    out = C.build_chips("002361.SZ")
    assert out["ok"] is True
    assert out["main_force"]["cyw"] is None          # 取不到就是 None, 不是 0
    assert any("CYW" in f for f in out["failed_formulas"])
    assert "CYW" not in out["formulas"]


def test_formula_engine_down_is_honest(monkeypatch):
    import marketdata.vendors.tq as T  # noqa: N812

    def boom(*a, **k):
        raise RuntimeError("TQ 客户端未运行")

    monkeypatch.setattr(T, "formula_zb_many", boom)
    out = C.build_chips("002361.SZ")
    assert out["ok"] is False and out["reason"] == "tq_unavailable"


def test_render_text_shows_dash_never_zero(monkeypatch):
    import marketdata.vendors.tq as T  # noqa: N812

    monkeypatch.setattr(T, "formula_zb_many", lambda names, code, count=250: _fake_raw(CYW={"_error": "x"}))
    txt = C.render_text(C.build_chips("002361.SZ", quote={"close": 10.14}))
    assert "筹码峰成本 SSRP：10.46" in txt
    assert "主力控盘 CYW：—" in txt
    assert "不是 0" in txt and "不构成买卖建议" in txt


def test_render_text_empty_when_not_ok():
    assert C.render_text({"ok": False}) == ""
