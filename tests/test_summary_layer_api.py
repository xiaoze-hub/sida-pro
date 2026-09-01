# -*- coding: utf-8 -*-
"""summary API 图层数据(gs_signals / fund_flow / events) + gs_strategy 序列函数 单测。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import gs_strategy  # noqa: E402
from src.web.api import klines as kapi  # noqa: E402
from src.core import decision_pioneer as dp  # noqa: E402


def _mk_bars(n=40, trend=0.1, final_surge=False):
    bars = []
    price = 20.0
    for i in range(n):
        o = price
        price += trend
        c = price
        bars.append({"date": f"2026-07-{i+1:02d}", "open": o,
                     "high": max(o, c) + 0.05, "low": min(o, c) - 0.05,
                     "close": c, "volume": 100000})
    if final_surge:
        o = bars[-1]["close"]
        c = o * 1.20
        bars.append({"date": "2026-08-31", "open": o, "high": c + 0.05,
                     "low": o - 0.05, "close": c, "volume": 500000})
    return bars


# ---------------------------------------------------------------------------
# compute_gs_signals 序列
# ---------------------------------------------------------------------------


def test_gs_signals_series_confirmed_flags():
    """末根交叉 confirmed=False(待确认), 历史交叉 confirmed=True。"""
    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    sigs = gs_strategy.compute_gs_signals(bars)
    assert sigs, "应有信号"
    last = sigs[-1]
    assert last["side"] == "G"
    assert last["confirmed"] is False
    assert all(s["confirmed"] for s in sigs[:-1])
    assert all(s["side"] in ("G", "S") for s in sigs)
    assert all("date" in s and "price" in s for s in sigs)


def test_gs_signals_insufficient_bars_empty():
    assert gs_strategy.compute_gs_signals(_mk_bars(n=10)) == []
    assert gs_strategy.compute_gs_signals([]) == []


def test_gs_signals_matches_eval_gs_latest():
    """序列的最后一条与 eval_gs 的最近信号一致(同一公式)。"""
    bars = _mk_bars(n=45, trend=-0.05, final_surge=True)
    sigs = gs_strategy.compute_gs_signals(bars)
    ev = gs_strategy.eval_gs(bars)
    if sigs:
        assert sigs[-1]["side"] == ev["signal"]


# ---------------------------------------------------------------------------
# _build_layer_data
# ---------------------------------------------------------------------------


def _patch_bars(monkeypatch, bars):
    monkeypatch.setattr(dp, "fetch_bars", lambda s, m, days=120: bars)


_ALL_NONE = {"gs_signals": None, "fund_flow": None, "events": None, "orderbook": None}


def test_build_layer_data_non_cn_returns_none():
    from src.models.market import MarketCode
    out = kapi._build_layer_data("000977", MarketCode.HK)
    assert out == _ALL_NONE


def test_build_layer_data_no_bars_returns_none(monkeypatch):
    from src.models.market import MarketCode
    _patch_bars(monkeypatch, [])
    out = kapi._build_layer_data("000977", MarketCode.CN)
    assert out == _ALL_NONE


def test_build_layer_data_full(monkeypatch):
    from src.models.market import MarketCode
    bars = _mk_bars(n=40, trend=-0.05, final_surge=True)
    _patch_bars(monkeypatch, bars)
    out = kapi._build_layer_data("000977", MarketCode.CN)

    # gs_signals: 有序列
    assert isinstance(out["gs_signals"], list)
    # fund_flow: 长度对齐 klines, dark_net 有值, ming_net 历史为 null
    assert len(out["fund_flow"]) == len(bars)
    assert all(f["ming_net"] is None for f in out["fund_flow"][:-1])
    assert any(f["dark_net"] is not None for f in out["fund_flow"])
    # events: 末根 +20% → 涨停
    assert any(e["kind"] == "limit_up" for e in out["events"])


def test_build_layer_data_events_limit_down(monkeypatch):
    from src.models.market import MarketCode
    bars = _mk_bars(n=40, trend=0.05)
    o = bars[-1]["close"]
    bars.append({"date": "2026-08-31", "open": o, "high": o + 0.05,
                 "low": o * 0.85 - 0.05, "close": o * 0.85, "volume": 500000})
    _patch_bars(monkeypatch, bars)
    out = kapi._build_layer_data("000977", MarketCode.CN)
    assert any(e["kind"] == "limit_down" for e in out["events"])


def test_tencent_code_normalizes():
    assert kapi._tencent_code("000977") == "sz000977"
    assert kapi._tencent_code("600103") == "sh600103"
    assert kapi._tencent_code("603893") == "sh603893"
    assert kapi._tencent_code("688981") == "sh688981"
    assert kapi._tencent_code("sz000977") == "sz000977"
    assert kapi._tencent_code("abc") is None
