"""腾讯证券数据源接入测试: 资金流 vendor + 盘口面板。"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent  # /tmp/PanWatch
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/marketdata/src"))

import pytest
from marketdata import Symbol
from marketdata.vendors.tencent_fundflow import TencentFundflowVendor, _tencent_code
from marketdata.vendors.tencent_panel import (
    fetch_pan_analysis,
    fetch_big_order_stats,
    fetch_price_distribution,
)


class TestTencentCode:
    def test_sz_code(self):
        assert _tencent_code(Symbol.parse("002361", "CN")) == "sz002361"

    def test_sh_code(self):
        assert _tencent_code(Symbol.parse("600519", "CN")) == "sh600519"

    def test_kcb_code(self):
        assert _tencent_code(Symbol.parse("688981", "CN")) == "sh688981"

    def test_invalid(self):
        assert _tencent_code(Symbol.parse("AAPL", "US")) is None


class TestTencentFundflowVendor:
    def test_fetch_real(self):
        """真实网络: 002361 应返回四档资金流。"""
        rows = TencentFundflowVendor().fetch([Symbol.parse("002361", "CN")], {})
        assert rows
        f = rows[0]
        assert f.main_net_inflow is not None
        assert f.super_net_inflow is not None
        assert f.big_net_inflow is not None

    def test_fetch_multiple(self):
        rows = TencentFundflowVendor().fetch(
            [Symbol.parse("002361", "CN"), Symbol.parse("600519", "CN")], {}
        )
        assert len(rows) == 2


class TestTencentPanel:
    def test_pan_analysis(self):
        pan = fetch_pan_analysis(Symbol.parse("002361", "CN"))
        assert pan is not None
        assert 0 <= pan["buy_big"] <= 100
        assert "buy_small" in pan and "sell_big" in pan and "sell_small" in pan

    def test_big_order_stats(self):
        stats = fetch_big_order_stats(Symbol.parse("002361", "CN"))
        assert stats is not None
        assert stats[0]["tier"] == 1
        assert stats[0]["amount_wan"] > 0

    def test_price_distribution(self):
        prices = fetch_price_distribution(Symbol.parse("002361", "CN"), limit=5)
        assert prices is not None
        assert len(prices) <= 5
        assert prices[0]["volume"] > 0
