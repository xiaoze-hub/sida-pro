"""指数 vs 个股的取数路由（2026-10-25 事故修复）。

**事故**：用户问「上证 7月到10月为什么跌」，参谋调用
`get_trend_profile("000001")` → 命中 `klines` 里的**平安银行**（现价 11.3），
于是把「60日 +5.31%」当成上证指数报给用户，得出「上证没有趋势下跌」。
实测上证 2026-07 的 4112 → 10-22 的 3658 = **-12%**，用户问的是对的。

本文件钉死：指数代码必须走 index_daily，绝不能落到 klines。
"""

from __future__ import annotations

import asyncio

import pytest

from src.agents.chat import registry as reg
from src.core import trend_profile as tp
from src.core.index_codes import INDEX_CODES, is_index_code


def test_指数代码表与首页指数条同源():
    for code in ("000001", "399001", "399006", "000300"):
        assert code in INDEX_CODES
        assert is_index_code(code, "CN")


def test_港股美股不按A股指数解析():
    assert not is_index_code("000001", "HK")
    assert not is_index_code("000001", "US")


def test_000001走index_daily不走klines(monkeypatch):
    calls = []

    def fake_index(code, days=250, session=None):
        calls.append(("index", code, days))
        return [{"date": "2026-10-22", "open": 3714, "high": 3714, "low": 3649,
                 "close": 3658, "volume": 4.3e10, "amount": None}]

    def fake_klines(symbol, market, days=250):
        calls.append(("klines", symbol, days))
        return [{"date": "2026-10-22", "open": 11, "high": 11.5, "low": 10.9,
                 "close": 11.3, "volume": 1000}]

    import src.core.index_daily as idx_mod
    import src.db.klines_repo as kr

    monkeypatch.setattr(idx_mod, "load_bars", fake_index)
    monkeypatch.setattr(kr, "load_qfq_bars_cached", fake_klines)

    bars = tp._load_bars("000001", "CN", 250)
    assert [c[0] for c in calls] == ["index"], f"000001 应只读 index_daily，实际 {calls}"
    assert bars[0]["close"] == 3658


def test_个股仍走klines(monkeypatch):
    calls = []

    def fake_index(code, days=250, session=None):
        calls.append("index")
        return []

    def fake_klines(symbol, market, days=250):
        calls.append("klines")
        return [{"date": "2026-10-22", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}]

    import src.core.index_daily as idx_mod
    import src.db.klines_repo as kr

    monkeypatch.setattr(idx_mod, "load_bars", fake_index)
    monkeypatch.setattr(kr, "load_qfq_bars_cached", fake_klines)

    bars = tp._load_bars("600111", "CN", 60)
    assert calls == ["klines"]
    assert bars[0]["close"] == 10.5


def test_区间工具已注册且描述里要求用区间而不是固定窗口():
    assert "get_range_summary" in reg.CHAT_TOOL_REGISTRY
    t = reg.CHAT_TOOL_REGISTRY["get_range_summary"]
    desc = t.schema["function"]["description"]
    assert "任意区间" in desc
    assert "不要用 get_trend_profile" in desc
    assert t.caliber.strip()


def test_区间工具缺参数要明确要(monkeypatch):
    out = asyncio.run(reg._tool_get_range_summary(None, {"symbol": "000001"}))
    assert "start_date" in out and "end_date" in out


def test_区间工具透传三个参数(monkeypatch):
    seen = {}

    def fake_build(symbol, start, end, market="CN"):
        seen.update(symbol=symbol, start=start, end=end, market=market)
        return "OK"

    monkeypatch.setattr(tp, "build_range_summary", fake_build)
    out = asyncio.run(reg._tool_get_range_summary(
        None, {"symbol": "000001", "start_date": "2026-07-01", "end_date": "2026-10-23"}))
    assert out == "OK"
    assert seen == {"symbol": "000001", "start": "2026-07-01", "end": "2026-10-23", "market": "CN"}
