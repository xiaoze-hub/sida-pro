"""tests for src/core/stock_link.py

audit P2 2026-09-15: stock_url 已删除(死函数), URL 生成测试改走 stock_link_markdown
或私有 _xueqiu_url。所有测试显式传入 platform 参数，避免访问数据库。
"""

from __future__ import annotations

from src.core.stock_link import stock_link_markdown, _xueqiu_url


class TestXueqiuUrl:
    def test_cn_sz(self):
        """股票链接 — CN 深圳股票"""
        url = _xueqiu_url("002837", "CN")
        assert url == "https://xueqiu.com/S/SZ002837"

    def test_cn_sh(self):
        """股票链接 — CN 上海股票"""
        url = _xueqiu_url("600519", "CN")
        assert url == "https://xueqiu.com/S/SH600519"

    def test_cn_bj(self):
        """股票链接 — CN 北交所股票"""
        url = _xueqiu_url("830799", "CN")
        assert url == "https://xueqiu.com/S/BJ830799"

    def test_us(self):
        """股票链接 — US 美股"""
        url = _xueqiu_url("AAPL", "US")
        assert url == "https://xueqiu.com/S/AAPL"

    def test_hk(self):
        """股票链接 — HK 港股"""
        url = _xueqiu_url("00883", "HK")
        assert url == "https://xueqiu.com/S/00883"


class TestStockLinkMarkdown:
    def test_cn(self):
        """Markdown 链接 — CN 格式"""
        md = stock_link_markdown("002837", "CN", platform="xueqiu")
        assert md == "[002837.CN](https://xueqiu.com/S/SZ002837)"

    def test_us(self):
        """Markdown 链接 — US 格式"""
        md = stock_link_markdown("AAPL", "US", platform="xueqiu")
        assert md == "[AAPL.US](https://xueqiu.com/S/AAPL)"

    def test_market_case_insensitive(self):
        """Markdown 链接 — 市场代码不区分大小写"""
        md = stock_link_markdown("002837", "cn", platform="xueqiu")
        assert md == "[002837.cn](https://xueqiu.com/S/SZ002837)"

    def test_hk(self):
        """Markdown 链接 — HK 格式"""
        md = stock_link_markdown("00883", "HK", platform="xueqiu")
        assert md == "[00883.HK](https://xueqiu.com/S/00883)"
