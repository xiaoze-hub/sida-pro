"""同码个股/指数必须使用显式类型并隔离缓存，不调用真实行情。"""
import asyncio
import json

from src.web.api import klines, quotes


def test_kline_instrument_has_separate_cache(monkeypatch):
    calls = []

    def fake(symbol, market, days, interval, instrument="auto"):
        calls.append(instrument)
        return {"instrument": instrument}

    monkeypatch.setattr(klines, "_get_klines_uncached", fake)
    monkeypatch.setattr(klines, "_kl_cache", {})
    assert klines.get_klines("000001", instrument="stock")["instrument"] == "stock"
    assert klines.get_klines("000001", instrument="index")["instrument"] == "index"
    assert klines.get_klines("000001", instrument="stock")["instrument"] == "stock"
    assert calls == ["stock", "index"]


def test_explicit_stock_000001_uses_pg_stock_path(monkeypatch):
    class UsedStockPath(Exception):
        pass

    def pg(symbol, market, days):
        assert symbol == "000001"
        raise UsedStockPath

    monkeypatch.setattr(klines, "_pg_klines", pg)
    import pytest
    with pytest.raises(UsedStockPath):
        klines._get_klines_uncached("000001", instrument="stock")


def test_minute_prefix_is_explicit_and_cache_isolated(monkeypatch):
    import urllib.request
    urls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return json.dumps({"data": {}}).encode()

    def urlopen(request, timeout):
        urls.append(request.full_url)
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    quotes._tencent_minute("000001", "CN", "stock")
    quotes._tencent_minute("000001", "CN", "index")
    assert urls[0].endswith("code=sz000001")
    assert urls[1].endswith("code=sh000001")
    monkeypatch.setattr(quotes, "_MINUTE_CACHE", {})
    monkeypatch.setattr(quotes, "_tencent_minute", lambda symbol, market, instrument: ([], 11 if instrument == "stock" else 3600, None))
    from src.core import rally_analysis
    monkeypatch.setattr(rally_analysis, "analyze_swings", lambda *_args: None)
    stock = asyncio.run(quotes.get_minute("000001", instrument="stock"))
    index = asyncio.run(quotes.get_minute("000001", instrument="index"))
    assert not stock["is_index"] and stock["prev_close"] == 11
    assert index["is_index"] and index["prev_close"] == 3600
