"""集合竞价逐票查询（2026-10-20 修复）。

用户报障：「查不到持仓或自选股的集合竞价情况」。根因不是没数据 —— `auction_collector`
里早就有 `fetch_auction_snapshots_tq/thsdk`，但 `_fetch_auction_context` 把
`scene="watchlist"` **静默别名成 strongest（全市场前 N）**，于是问到"我的票"时只能得到
"不在榜单里"，被模型讲成"没有竞价强度/没有数据"。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.collectors import auction_collector as ac
from src.web.api import chat as chat_api


def test_watchlist_scene_uses_per_symbol_not_strongest(monkeypatch):
    calls = {}

    def _boom(*a, **k):  # 断言不再走全市场榜单
        raise AssertionError("watchlist 不应再走 strongest")

    def _fake_symbols(symbols, limit=20):
        calls["symbols"] = list(symbols)
        return "PER_SYMBOL_OK"

    monkeypatch.setattr(ac, "fetch_auction_strongest", _boom)
    monkeypatch.setattr(ac, "fetch_auction_symbols", _fake_symbols)

    class _Q:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def limit(self, n):
            return self

        def all(self):
            return self.rows

    class _DB:
        def query(self, model):
            return _Q([SimpleNamespace(symbol="600111"), SimpleNamespace(symbol="002361")])

    user = SimpleNamespace(id="u1")
    out = asyncio.run(chat_api._fetch_auction_context("watchlist", 10, db=_DB(), user=user))
    assert out == "PER_SYMBOL_OK"
    assert calls["symbols"] == ["600111", "002361"]


def test_explicit_symbols_take_priority(monkeypatch):
    seen = {}

    def _fake_symbols(symbols, limit=20):
        seen["symbols"] = symbols
        return "OK"

    monkeypatch.setattr(ac, "fetch_auction_symbols", _fake_symbols)
    out = asyncio.run(chat_api._fetch_auction_context("symbols", 10, symbols=["600111"]))
    assert out == "OK" and seen["symbols"] == ["600111"]


def test_no_symbols_and_no_watchlist_says_so():
    class _Q:
        def filter(self, *a, **k):
            return self

        def order_by(self, *a, **k):
            return self

        def limit(self, n):
            return self

        def all(self):
            return []

    class _DB:
        def query(self, model):
            return _Q()

    out = asyncio.run(chat_api._fetch_auction_context("watchlist", 10, db=_DB(), user=SimpleNamespace(id="u")))
    assert "没有可查的标的" in out


def test_renderer_gives_every_symbol_a_line(monkeypatch):
    def _fake_tq(symbols, limit=20):
        return {
            "600111": {
                "source": "tq", "last_close": 35.23, "open": 35.20, "open_pct": -0.09,
                "open_amount": 4.2e7, "open_limit_buy": None,
                "buy1": "35.20", "buy1_vol": "322", "sell1": "35.21", "sell1_vol": "91",
            }
        }

    monkeypatch.setattr(ac, "fetch_auction_snapshots_tq", _fake_tq)
    monkeypatch.setattr(ac, "fetch_auction_snapshots_thsdk", lambda *a, **k: {})

    text = ac.fetch_auction_symbols(["600111", "002361"])
    assert "600111" in text and "竞价成交额 4200 万" in text and "-0.09%" in text
    # 取不到的票要明写取不到，不能默默消失
    assert "002361: 无竞价快照" in text
    assert "≠「竞价没强度」" in text
