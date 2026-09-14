# -*- coding: utf-8 -*-
"""KI-043/044/045: degraded 标记。"""
import asyncio
from unittest.mock import MagicMock, patch

from src.web.api import stocks as stocks_mod


def test_get_quotes_degraded_markets():
    db = MagicMock()
    user = MagicMock()
    user.id = 1
    stock = MagicMock()
    stock.market = "CN"
    stock.symbol = "600519"
    q = MagicMock()
    q.filter.return_value.all.return_value = [stock]
    db.query.return_value = q

    with patch.object(stocks_mod, "md_quote_rows", side_effect=ConnectionError("vendor down")):
        data = stocks_mod.get_quotes(db=db, user=user)

    assert "quotes" in data
    assert data["quotes"] == {}
    assert data["degraded_markets"][0]["market"] == "CN"
    assert "vendor down" in data["degraded_markets"][0]["error"]


def test_board_flow_empty_no_stale_is_degraded():
    from src.web.api import market_data as md

    with patch.object(md, "_stale_take", return_value=None):
        class FakeMd:
            def board_capital_flow(self, board_type="industry"):
                return []

        with patch("src.core.marketdata_client.get_market_data", return_value=FakeMd()):
            # board_capital_flow_proxy is async
            payload = asyncio.run(md.board_capital_flow_proxy(board_type="industry"))
    assert payload["count"] == 0
    assert payload["degraded"] is True
    assert "不是" in payload["note"] or "非" in payload["note"]


def test_news_timeout_is_degraded():
    from src.web.api import news as news_mod

    class FakeCollector:
        async def fetch_all(self, **kw):
            raise TimeoutError("slow")

        @classmethod
        def from_database(cls):
            return cls()

    db = MagicMock()
    user = MagicMock()
    user.id = 1
    # 自选非空: 走采集路径
    stock = MagicMock()
    stock.symbol = "600519"
    stock.name = "贵州茅台"
    q = MagicMock()
    q.filter.return_value.all.return_value = [stock]
    db.query.return_value = q

    with patch.object(news_mod, "NewsCollector", FakeCollector):
        payload = asyncio.run(news_mod.get_news(
            symbols="", names="", hours=168, limit=50,
            filter_related=False, source="", db=db, user=user,
        ))
    assert payload["degraded"] is True
    assert payload["items"] == []
    assert payload["note"]


def test_news_empty_watchlist_is_not_degraded():
    """无自选 → 空 items 且 degraded=false(真空态, 非源故障)。"""
    from src.web.api import news as news_mod

    db = MagicMock()
    user = MagicMock()
    user.id = 1
    q = MagicMock()
    q.filter.return_value.all.return_value = []
    db.query.return_value = q

    payload = asyncio.run(news_mod.get_news(
        symbols="", names="", hours=168, limit=50,
        filter_related=False, source="", db=db, user=user,
    ))
    assert payload == {"items": [], "degraded": False, "note": None}
