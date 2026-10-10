"""决策先锋辅助指标测试(P3 补差, 2026-10-10) —— 趋势操盘线 / 牛熊线 / 分时突破。

**禁真网络**: 纯计算用例直接喂合成 bar; API 契约用例 monkeypatch 取数函数(不触网/库)。
覆盖每指标: 正例 / 反例 / 边界(数据不足/一字板/停牌/缺输入降级) + 买卖点触发条件断言 +
API 契约 + 显式降级。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _bars(rows):
    """rows: [(o, h, l, c), ...] → bar dict 列表。"""
    return [{"open": o, "high": h, "low": l, "close": c} for o, h, l, c in rows]


# ══════════════════════════════════════════════════════════════════════
# 阈值配置层: 新键默认值 + env 覆盖(P3 参数可调)
# ══════════════════════════════════════════════════════════════════════
def test_trend_threshold_keys_defaults_and_env(monkeypatch):
    from src.core import thresholds

    snap = thresholds.snapshot()
    for key in (
        "trend_pilot_red_period", "trend_pilot_yellow_period", "trend_pilot_green_period",
        "trend_pilot_band_tol_pct",
        "niuxiong_bull_period", "niuxiong_horse_period", "niuxiong_trade_period",
        "minute_consolidation_min", "minute_volume_spike_mult", "minute_dde_min_wan",
    ):
        assert key in snap, f"缺少配置键 {key}"
        assert snap[key]["source"] == "default"

    # 红线=快(10) < 黄线=慢(20) < 绿线=长(60): 多头排列方向正确
    assert thresholds.value("trend_pilot_red_period") < thresholds.value("trend_pilot_yellow_period")
    assert thresholds.value("trend_pilot_yellow_period") < thresholds.value("trend_pilot_green_period")

    monkeypatch.setenv("SIDA_THRESHOLD_TREND_PILOT_GREEN_PERIOD", "45")
    assert thresholds.value("trend_pilot_green_period") == 45
    assert thresholds.source("trend_pilot_green_period") == "env"


# ══════════════════════════════════════════════════════════════════════
# 趋势操盘线
# ══════════════════════════════════════════════════════════════════════
_P_TREND = {"red_period": 3, "yellow_period": 5, "green_period": 10}


def _trend_up_with_pullback():
    rows = []
    c = 10.0
    for i in range(20):
        c = 10 + i * 0.5
        rows.append((c - 0.05, c + 0.15, c - 0.15, c))
    o2 = rows[-1][3] - 0.1
    rows[-1] = (o2, o2 + 0.05, o2 - 0.6, o2 + 0.1)  # 回踩(下探带区)收阳
    return _bars(rows)


def test_trend_line_insufficient_returns_none():
    from src.core.trend_pilot_line import compute_trend_line

    assert compute_trend_line([]) is None
    assert compute_trend_line(_bars([(10, 10.1, 9.9, 10)])) is None
    # 默认参数需 61 根 → 20 根返回 None(数据不足, 不编造)
    assert compute_trend_line(_bars([(10, 10.1, 9.9, 10)] * 20)) is None


def test_trend_line_reads_thresholds_config(monkeypatch):
    """默认周期(10/20/60)不足 → None; env 缩短后同批 bar 可算且 params 反映 env。"""
    from src.core import thresholds
    from src.core.trend_pilot_line import compute_trend_line

    for k in ("trend_pilot_red_period", "trend_pilot_yellow_period", "trend_pilot_green_period"):
        monkeypatch.delenv(thresholds.snapshot()[k]["env"], raising=False)
    bars = _bars([(10 + i * 0.4, 10.4 + i * 0.4, 9.9 + i * 0.4, 10.2 + i * 0.4) for i in range(8)])
    assert compute_trend_line(bars) is None  # 默认需 61 根

    monkeypatch.setenv("SIDA_THRESHOLD_TREND_PILOT_RED_PERIOD", "3")
    monkeypatch.setenv("SIDA_THRESHOLD_TREND_PILOT_YELLOW_PERIOD", "4")
    monkeypatch.setenv("SIDA_THRESHOLD_TREND_PILOT_GREEN_PERIOD", "6")
    r = compute_trend_line(bars)
    assert r is not None
    assert r["params"]["red_period"] == 3
    assert r["params"]["yellow_period"] == 4
    assert r["params"]["green_period"] == 6


def test_trend_line_buy_pullback_bull_band():
    from src.core.trend_pilot_line import compute_trend_line

    r = compute_trend_line(_trend_up_with_pullback(), _P_TREND)
    assert r["band"]["state"] == "多方带"
    assert r["trend"] == "升势"
    rules = [b["rule"] for b in r["buy_points"]]
    assert "pullback_bull_band_yang" in rules
    bp = next(b for b in r["buy_points"] if b["rule"] == "pullback_bull_band_yang")
    assert bp["signal"] == "buy" and bp["price"] and "回踩" in bp["trigger"]


def test_trend_line_buy_pullback_green_yang():
    from src.core.trend_pilot_line import compute_trend_line

    rows = []
    c = 10.0
    for i in range(22):
        c = 10 + i * 0.4
        rows.append((c - 0.05, c + 0.15, c - 0.1, c))
    # 深回踩: 低点触绿线区, 收阳且收于绿线上方
    rows[-1] = (16.0, 16.9, 15.9, 16.8)
    r = compute_trend_line(_bars(rows), _P_TREND)
    assert r is not None
    assert "pullback_green_yang" in [b["rule"] for b in r["buy_points"]]


def test_trend_line_sell_rebound_green_fail():
    from src.core.trend_pilot_line import compute_trend_line

    rows = []
    c = 20.0
    for i in range(20):
        c = 20 - i * 0.15
        rows.append((c + 0.05, c + 0.1, c - 0.1, c))
    o3 = rows[-1][3]
    rows[-1] = (o3 + 0.02, o3 + 0.75, o3 - 0.05, o3 - 0.02)  # 反弹触绿线但收在绿线下
    r = compute_trend_line(_bars(rows), _P_TREND)
    assert r["trend"] == "跌势"
    assert "rebound_green_fail" in [s["rule"] for s in r["sell_points"]]
    sp = r["sell_points"][0]
    assert sp["signal"] == "sell" and "无力突破" in sp["trigger"]


def test_trend_line_no_signal_when_no_pullback():
    from src.core.trend_pilot_line import compute_trend_line

    # 单边强势上行, 不回踩 → 无买卖点
    rows = [(10 + i * 0.5 - 0.05, 10 + i * 0.5 + 0.3, 10 + i * 0.5 - 0.1, 10 + i * 0.5) for i in range(20)]
    r = compute_trend_line(_bars(rows), _P_TREND)
    assert r["buy_points"] == [] and r["sell_points"] == []


def test_trend_line_flat_limit_board_no_signal():
    """一字板(h==l)不产生任何买卖点(避免零振幅假信号)。"""
    from src.core.trend_pilot_line import compute_trend_line

    r = compute_trend_line(_bars([(10, 10, 10, 10)] * 15), _P_TREND)
    assert r["buy_points"] == [] and r["sell_points"] == []


def test_trend_line_signal_is_evidence_not_advice():
    from src.core.trend_pilot_line import compute_trend_line

    r = compute_trend_line(_trend_up_with_pullback(), _P_TREND)
    for s in r["signals"]:
        assert s["signal"] in ("buy", "sell")
        assert "建议" not in s["trigger"]
        assert s["rule"] and s["trigger"] and s["price"] is not None
    assert "待校准" in r["calibration"]


# ══════════════════════════════════════════════════════════════════════
# 牛熊线
# ══════════════════════════════════════════════════════════════════════
_P_NIX = {"bull_period": 3, "horse_period": 2, "trade_period": 6}


def _closes_to_bars(closes):
    return [{"close": c} for c in closes]


def test_niuxiong_insufficient_returns_none():
    from src.core.niuxiong_line import compute_niuxiong

    assert compute_niuxiong([]) is None
    assert compute_niuxiong(_closes_to_bars([10, 10, 10])) is None
    # 默认(牛20/买卖30)需 32 根 → 10 根 None
    assert compute_niuxiong(_closes_to_bars([10 + i for i in range(10)])) is None


def test_niuxiong_reads_thresholds_config(monkeypatch):
    from src.core import thresholds
    from src.core.niuxiong_line import compute_niuxiong

    for k in ("niuxiong_bull_period", "niuxiong_horse_period", "niuxiong_trade_period"):
        monkeypatch.delenv(thresholds.snapshot()[k]["env"], raising=False)
    bars = _closes_to_bars([10 + i * 0.3 for i in range(12)])
    assert compute_niuxiong(bars) is None  # 默认需 32 根
    monkeypatch.setenv("SIDA_THRESHOLD_NIUXIONG_BULL_PERIOD", "3")
    monkeypatch.setenv("SIDA_THRESHOLD_NIUXIONG_HORSE_PERIOD", "2")
    monkeypatch.setenv("SIDA_THRESHOLD_NIUXIONG_TRADE_PERIOD", "6")
    r = compute_niuxiong(bars)
    assert r is not None
    assert r["params"] == {"bull_period": 3, "horse_period": 2, "trade_period": 6}


def test_niuxiong_golden_cross_b_buy_red():
    from src.core.niuxiong_line import compute_niuxiong

    closes = []
    c = 20.0
    for _ in range(25):
        c -= 0.2
        closes.append(round(c, 4))
    for _ in range(20):
        c += 0.4
        closes.append(round(c, 4))
    r = compute_niuxiong(_closes_to_bars(closes), _P_NIX)
    assert r["signal"] == "B"
    assert r["color"] == "red"          # 买红
    assert r["cross"]["type"] == "golden"
    assert r["cross"]["bars_ago"] is not None
    assert r["state"] == "牛线上方"


def test_niuxiong_death_cross_s_sell_green():
    from src.core.niuxiong_line import compute_niuxiong

    closes = []
    c = 10.0
    for _ in range(25):
        c += 0.2
        closes.append(round(c, 4))
    for _ in range(20):
        c -= 0.4
        closes.append(round(c, 4))
    r = compute_niuxiong(_closes_to_bars(closes), _P_NIX)
    assert r["signal"] == "S"
    assert r["color"] == "green"        # 卖绿
    assert r["cross"]["type"] == "death"
    assert r["state"] == "牛线下方"


def test_niuxiong_wma_weighting():
    """加权均线: 线性权重 1..n, 最近值权重最大。"""
    from src.core.niuxiong_line import wma_series

    # [1,2,3] 周期3: (1*1+2*2+3*3)/6 = 14/6
    assert wma_series([1, 2, 3], 3)[-1] == pytest.approx(14 / 6)
    assert wma_series([1, 2], 3) == [0.0, 0.0]  # 不足周期占位 0


def test_niuxiong_flat_no_cross():
    from src.core.niuxiong_line import compute_niuxiong

    r = compute_niuxiong(_closes_to_bars([10.0] * 12), _P_NIX)
    assert r is not None
    assert r["signal"] is None and r["color"] is None
    assert r["cross"]["type"] is None


def test_niuxiong_signal_is_evidence_not_advice():
    from src.core.niuxiong_line import compute_niuxiong

    r = compute_niuxiong(_closes_to_bars([10 + i * 0.2 for i in range(12)]), _P_NIX)
    assert "建议" not in str(r)
    assert "待校准" in r["calibration"]


# ══════════════════════════════════════════════════════════════════════
# 分时突破「突/积」
# ══════════════════════════════════════════════════════════════════════
def _mb(t, o, h, l, c, v):
    return {"time": t, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _tu_bars():
    """16 分钟窄幅盘整(09:30..09:45) + 09:46 放量突破前高。"""
    bars = [_mb(f"09:{30 + i:02d}", 10.0, 10.02, 9.99, 10.0, 1000) for i in range(16)]
    bars.append(_mb("09:46", 10.0, 10.4, 9.99, 10.35, 5000))
    return bars


def _dde_for(bars, main_net, times=None):
    times = set(times) if times else None
    return [{"time": b["time"], "main_net": (main_net if (times is None or b["time"] in times) else 0)}
            for b in bars]


def test_minute_breakthrough_tu_positive():
    from src.core.minute_breakthrough import compute_breakthrough

    bars = _tu_bars()
    dde = _dde_for(bars, main_net=1_000_000, times=("09:44", "09:45", "09:46"))
    r = compute_breakthrough(bars, dde)
    assert r["available"] is True and r["degraded"] is False
    assert r["signal_type"] == "突"
    assert r["trigger_time"] == "09:46"
    names = {c["name"] for c in r["conditions"]}
    assert {"盘整>15分钟", "突然放量异动", "突破日内高点", "DDE大单持续流入"} <= names
    assert all(c["met"] for c in r["conditions"])


def test_minute_breakthrough_ji_positive():
    from src.core.minute_breakthrough import compute_breakthrough

    bars = [_mb(f"09:{30 + i:02d}", 10.0, 10.01, 9.99, 10.0, 1000) for i in range(20)]
    dde = _dde_for(bars, main_net=200_000)  # 早盘每分钟 20 万, 稳健流入
    r = compute_breakthrough(bars, dde)
    assert r["signal_type"] == "积"
    assert r["trigger_time"] is not None
    assert "早盘大单稳健流入" in r["met_conditions"]


def test_minute_breakthrough_no_signal_on_negative_inflow():
    from src.core.minute_breakthrough import compute_breakthrough

    bars = [_mb(f"09:{30 + i:02d}", 10.0, 10.01, 9.99, 10.0, 1000) for i in range(20)]
    dde = _dde_for(bars, main_net=-200_000)
    r = compute_breakthrough(bars, dde)
    assert r["available"] is True
    assert r["signal_type"] is None


def test_minute_breakthrough_volume_not_spiking_no_tu():
    from src.core.minute_breakthrough import compute_breakthrough

    bars = _tu_bars()
    bars[-1]["volume"] = 1000  # 无量 → 不构成「突」
    dde = _dde_for(bars, main_net=1_000_000, times=("09:44", "09:45", "09:46"))
    r = compute_breakthrough(bars, dde)
    assert r["signal_type"] != "突"


def test_minute_breakthrough_degraded_missing_dde():
    from src.core.minute_breakthrough import compute_breakthrough

    r = compute_breakthrough(_tu_bars(), None)
    assert r["available"] is False and r["degraded"] is True
    assert r["signal_type"] is None
    assert any("DDE" in x for x in r["reasons"])


def test_minute_breakthrough_degraded_missing_minutes():
    from src.core.minute_breakthrough import compute_breakthrough

    dde = [{"time": "09:30", "main_net": 1_000_000}]
    r = compute_breakthrough([], dde)
    assert r["degraded"] is True
    assert any("分钟数据" in x for x in r["reasons"])


def test_minute_breakthrough_degraded_too_few_bars():
    from src.core.minute_breakthrough import compute_breakthrough

    bars = [_mb(f"09:3{i}", 10.0, 10.01, 9.99, 10.0, 1000) for i in range(5)]
    dde = _dde_for(bars, main_net=1_000_000)
    r = compute_breakthrough(bars, dde)
    assert r["degraded"] is True
    assert any("分钟数据不足" in x for x in r["reasons"])


def test_minute_breakthrough_flat_limit_no_tu():
    """一字板(无新高)不构成「突」。"""
    from src.core.minute_breakthrough import compute_breakthrough

    bars = [_mb(f"09:{30 + i:02d}", 10.0, 10.0, 10.0, 10.0, 1000) for i in range(18)]
    dde = _dde_for(bars, main_net=1_000_000)
    r = compute_breakthrough(bars, dde)
    assert r["signal_type"] != "突"


def test_minute_breakthrough_reads_thresholds_config(monkeypatch):
    from src.core import thresholds
    from src.core.minute_breakthrough import compute_breakthrough

    monkeypatch.setenv("SIDA_THRESHOLD_MINUTE_VOLUME_SPIKE_MULT", "10.0")
    # 放量 5 倍 < 10 倍 → 「突」被配置层挡住
    bars = _tu_bars()
    dde = _dde_for(bars, main_net=1_000_000, times=("09:44", "09:45", "09:46"))
    r = compute_breakthrough(bars, dde)
    assert r["params"]["volume_spike_mult"] == 10.0
    assert r["signal_type"] != "突"
    assert thresholds.source("minute_volume_spike_mult") == "env"


def test_minute_breakthrough_signal_is_evidence_not_advice():
    from src.core.minute_breakthrough import compute_breakthrough

    bars = _tu_bars()
    dde = _dde_for(bars, main_net=1_000_000, times=("09:44", "09:45", "09:46"))
    r = compute_breakthrough(bars, dde)
    assert r["signal_type"] in ("突", "积")
    assert "建议" not in str(r)


# ══════════════════════════════════════════════════════════════════════
# API 契约(monkeypatch 取数, 不触网/库)
# ══════════════════════════════════════════════════════════════════════
class _FakeOwner:
    id = "test-owner"
    username = "test-owner"
    role = "owner"


@pytest.fixture()
def client():
    from src.web.api import pioneer_indicators as pi
    from src.web.app import app
    from src.web.api.auth import get_current_user

    pi._CACHE.clear()
    app.dependency_overrides[get_current_user] = lambda: _FakeOwner()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        pi._CACHE.clear()


def test_trend_line_api_contract(client, monkeypatch):
    import src.core.trend_pilot_line as tpl

    monkeypatch.setattr(tpl, "fetch_trend_line", lambda *a, **k: {"available": True, "lines": {"red": 1.0, "yellow": 0.9, "green": 0.8}, "buy_points": [], "sell_points": [], "signals": []})
    r = client.get("/api/indicators/trend-line/600519")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["symbol"] == "600519" and d["indicator"] == "trend-line"
    assert d["available"] is True and d["lines"]["red"] == 1.0


def test_niuxiong_api_contract(client, monkeypatch):
    import src.core.niuxiong_line as nxl

    monkeypatch.setattr(nxl, "fetch_niuxiong", lambda *a, **k: {"available": True, "signal": "B", "color": "red", "cross": {"type": "golden", "bars_ago": 2}})
    r = client.get("/api/indicators/niuxiong/600519")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["indicator"] == "niuxiong" and d["signal"] == "B" and d["color"] == "red"


def test_minute_breakthrough_api_contract(client, monkeypatch):
    import src.core.minute_breakthrough as mbt

    monkeypatch.setattr(mbt, "fetch_minute_breakthrough", lambda *a, **k: {"available": False, "degraded": True, "signal_type": None, "reasons": ["DDE大单流入序列缺失"]})
    r = client.get("/api/indicators/minute-breakthrough/600519")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["indicator"] == "minute-breakthrough"
    assert d["available"] is False and d["degraded"] is True and d["signal_type"] is None


def test_indicators_api_unavailable_note(client, monkeypatch):
    import src.core.trend_pilot_line as tpl

    monkeypatch.setattr(tpl, "fetch_trend_line", lambda *a, **k: None)
    r = client.get("/api/indicators/trend-line/600519")
    assert r.status_code == 200
    d = r.json()["data"]
    assert d["available"] is False and d["degraded"] is True and d["note"]


@pytest.mark.parametrize("path", ["trend-line", "niuxiong", "minute-breakthrough"])
def test_indicators_api_invalid_symbol_400(client, path):
    r = client.get(f"/api/indicators/{path}/12ab")
    assert r.status_code == 400
