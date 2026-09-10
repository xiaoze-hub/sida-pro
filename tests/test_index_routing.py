"""指数取数(K线 + market.py /indices)路由测试"""
import asyncio

import pytest
from fastapi import HTTPException

import src.collectors.kline_collector as kc
import src.web.api.market as mkt


def test_get_index_klines_uses_marketdata(monkeypatch):
    """get_index_klines 走 md.index_klines(同一 INDEX_SECID 语义),转换为 KlineData。"""
    from marketdata.types import Bar

    captured: dict = {}

    class _MD:
        def index_klines(self, code, *, market, days):
            captured["code"] = code
            captured["market"] = market
            captured["days"] = days
            return [Bar(date="2026-07-01", open=3180.0, close=3200.0, high=3210.0, low=3170.0, volume=1e8)]

    monkeypatch.setattr(kc, "get_market_data", lambda: _MD())

    out = kc.get_index_klines("000001", kc.MarketCode.CN, days=120)

    assert captured == {"code": "000001", "market": "CN", "days": 120}
    assert len(out) == 1 and isinstance(out[0], kc.KlineData)
    assert out[0].date == "2026-07-01" and out[0].close == 3200.0


def test_get_market_indices_uses_marketdata(monkeypatch):
    """/indices 走 md.index_quotes;quote_map/response_symbol 匹配逻辑与返回字段不变。"""
    captured: dict = {}

    class _MD:
        def index_quotes(self, tencent_symbols):
            captured["symbols"] = list(tencent_symbols)
            return [
                {
                    "symbol": "000001",
                    "name": "上证指数",
                    "current_price": 3200.0,
                    "change_pct": 0.63,
                    "change_amount": 20.0,
                    "prev_close": 3180.0,
                },
            ]

    monkeypatch.setattr(mkt, "get_market_data", lambda: _MD())
    # spark 取数不是本用例关注点,桩掉避免真实联网(见 test_market_indices_spark.py 专测 spark)。
    monkeypatch.setattr(mkt, "get_index_klines", lambda *a, **k: [])

    out = asyncio.run(mkt.get_market_indices())

    assert captured["symbols"] == [idx["tencent_symbol"] for idx in mkt.MARKET_INDICES]
    sh = next(i for i in out if i["symbol"] == "000001")
    assert sh["current_price"] == 3200.0 and sh["change_pct"] == 0.63
    # 未命中行情的指数仍返回基本信息占位(current_price=None),匹配逻辑不变
    hsi = next(i for i in out if i["symbol"] == "HSI")
    assert hsi["current_price"] is None


def test_get_market_indices_explicit_502_when_all_empty(monkeypatch):
    """C4 单源审计: 源全空 → 显式 502(不再 return [] 让首页指数条静默消失)。"""
    mkt.clear_indices_cache()

    class _MD:
        def index_quotes(self, tencent_symbols):
            return []

    monkeypatch.setattr(mkt, "get_market_data", lambda: _MD())
    monkeypatch.setattr(mkt, "get_index_klines", lambda *a, **k: [])

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mkt.get_market_indices())
    assert ei.value.status_code == 502


def test_get_market_indices_explicit_502_on_source_exception(monkeypatch):
    """C4 单源审计: 取数抛异常 → 显式 502(原来是吞掉返回 []), 前端走 ErrorBanner 重试。"""
    mkt.clear_indices_cache()

    class _MD:
        def index_quotes(self, tencent_symbols):
            raise RuntimeError("boom")

    monkeypatch.setattr(mkt, "get_market_data", lambda: _MD())
    monkeypatch.setattr(mkt, "get_index_klines", lambda *a, **k: [])

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mkt.get_market_indices())
    assert ei.value.status_code == 502
