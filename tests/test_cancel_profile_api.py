# -*- coding: utf-8 -*-
"""I-3 委托行为画像 API 测试: 路由挂载 + 序列/结论出参 + 意图判定联动 + 降级诚实.

不触网: `fetch_cancel_profile` 被 monkeypatch 成合成序列（真实算法仍跑 build_cancel_profile）。
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from src.core.cancel_profile import build_cancel_profile
from src.web.api import cancel_profile as api


def _rows(baseline_n: int = 60, today_rate: float = 0.62) -> list[dict]:
    base = date(2026, 7, 1)
    rows = []
    for i in range(baseline_n):
        d = base + timedelta(days=i)
        rate = 0.30 if i % 2 == 0 else 0.50
        rows.append({"Date": d.strftime("%Y%m%d"), "BCancel": rate * 100000,
                     "BOrder": 100000.0, "SCancel": 35000.0, "SOrder": 100000.0})
    d = base + timedelta(days=baseline_n)
    rows.append({"Date": d.strftime("%Y%m%d"), "BCancel": today_rate * 100000,
                 "BOrder": 100000.0, "SCancel": 35000.0, "SOrder": 100000.0})
    return rows


def _profile(pct_chg=None):
    return build_cancel_profile(_rows(), window=60, pct_chg=pct_chg)


def test_route_is_mounted_at_api_market_prefix():
    """路径必须是 /api/market/cancel-profile/{symbol}（挂载前缀 + 路由拼接）。"""
    from src.web.app import app

    paths = app.openapi()["paths"]
    assert "/api/market/cancel-profile/{symbol}" in paths
    assert "get" in paths["/api/market/cancel-profile/{symbol}"]


def test_handler_returns_series_baseline_and_conclusion(monkeypatch):
    monkeypatch.setattr(api, "fetch_cancel_profile", lambda *a, **kw: _profile(pct_chg=kw.get("pct_chg")))

    res = asyncio.run(api.get_cancel_profile("002361", market="CN", days=60, pct_chg=4.2))

    assert res["available"] is True
    assert res["symbol"] == "002361"
    assert len(res["series"]) == 61                     # 60 基线日 + 今日
    assert res["series"][-1]["date"] == res["as_of"]
    assert res["baseline_days"] == 60
    assert res["buy"]["median"] == pytest.approx(0.40)
    assert res["buy"]["z"] > 2.0
    assert res["buy"]["percentile"] == 100.0
    assert res["verdict"]["code"] == "fake_bidding"
    assert "今日买撤 62.0%" in res["conclusion"]


def test_handler_wires_baseline_into_micro_intent(monkeypatch):
    """同一份今日日线统计要能跑出相对 z 口径的微观意图（I-3 对意图判定的升级）。"""
    monkeypatch.setattr(api, "fetch_cancel_profile", lambda *a, **kw: _profile(pct_chg=kw.get("pct_chg")))

    res = asyncio.run(api.get_cancel_profile("002361", market="CN", days=60, pct_chg=4.2))

    intent = res["intent"]
    assert intent is not None
    assert intent["decision_basis"] == "baseline_z"
    assert intent["baseline_days"] == 60
    assert intent["intent_tag"] == "⚠️ 相对异常诱多"


def test_handler_normal_day_intent_is_not_warning(monkeypatch):
    monkeypatch.setattr(api, "fetch_cancel_profile",
                        lambda *a, **kw: _profile(pct_chg=kw.get("pct_chg")))

    res = asyncio.run(api.get_cancel_profile("002361", market="CN", days=60, pct_chg=0.8))
    assert res["verdict"]["code"] == "elevated_no_rally"
    assert res["intent"]["warning_level"] == "caution"


def test_handler_degrades_when_vendor_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("TQ 客户端未启动")

    monkeypatch.setattr(api, "fetch_cancel_profile", boom)
    res = asyncio.run(api.get_cancel_profile("002361", market="CN", days=60, pct_chg=None))

    assert res["available"] is False
    assert res["series"] == []
    assert res["intent"] is None
    assert "TQ 客户端未启动" in res["reason"]


def test_handler_non_cn_market_is_unavailable_but_well_shaped():
    res = asyncio.run(api.get_cancel_profile("00700", market="HK", days=60, pct_chg=None))
    assert res["available"] is False
    assert "仅支持 A 股" in res["reason"]
    assert res["verdict"]["code"] == "insufficient"


def test_handler_blank_symbol_is_unavailable():
    res = asyncio.run(api.get_cancel_profile("  ", market="CN", days=60, pct_chg=None))
    assert res["available"] is False
    assert res["reason"]
