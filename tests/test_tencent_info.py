"""腾讯证券深挖接口测试(2026-08-11): 公告/股东/简况/行业排名/热门榜。"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent  # /tmp/PanWatch
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/marketdata/src"))

import pytest
from marketdata import Symbol
from marketdata.vendors.tencent_info import (
    fetch_notice_list,
    fetch_news_list,
    fetch_top_holders,
    fetch_org_rating,
    fetch_industry_rank,
    fetch_stock_brief,
    fetch_plate_list,
    fetch_investment,
    fetch_hot_rank,
)

S = Symbol.parse("002361", "CN")


class TestTencentInfo:
    def test_notice_list(self):
        items = fetch_notice_list(S, 3)
        assert items is not None
        assert items[0]["title"]
        assert items[0]["time"]

    def test_news_list(self):
        items = fetch_news_list(S, 3)
        assert items is not None
        assert items[0]["title"]

    def test_top_holders(self):
        rows = fetch_top_holders(S, 3)
        assert rows is not None
        assert rows[0]["name"]
        assert rows[0]["ratio"] is not None

    def test_org_rating(self):
        d = fetch_org_rating(S)
        assert d is not None
        assert "pjtj1" in d or "mbjj" in d

    def test_industry_rank(self):
        d = fetch_industry_rank(S)
        assert d is not None
        assert "hyinfo" in d or "pm" in d

    def test_stock_brief(self):
        d = fetch_stock_brief(S)
        assert d is not None
        assert "zyzb" in d

    def test_plate_list(self):
        d = fetch_plate_list(S)
        assert d is not None
        assert "concept" in d or "area" in d

    def test_investment(self):
        items = fetch_investment(S)
        assert items is not None
        assert len(items) > 0

    def test_hot_rank(self):
        rows = fetch_hot_rank(3)
        assert rows is not None
        assert rows[0]["name"]
        assert rows[0]["zdf"] is not None
