"""风险方案1.1 (B2): vendor 缺失字段必须返回 None + status 标记, 禁止回退 0。

红线: 数据缺失显式标注「无数据」。0 价格参与涨跌幅算术会伪造 -100% 假暴跌。
"""

from __future__ import annotations

from marketdata.types import Quote
from marketdata.vendors.tencent import _parse_line
from marketdata.vendors.zhitu_full import ZhituCapitalFlowVendor, ZhituKlineVendor, _num


def _tencent_line(overrides: dict | None = None) -> str:
    """构造一条 tencent 原始报文(50 段, 对齐真实格式)。overrides 按 parts 下标覆盖。

    真实样例(2026-09-08 实抓):
    v_sh600519="1~贵州茅台~600519~1309.30~1316.01~1310.00~17534~...~1309.30/17534/2302823753~..."
    """
    parts = ["1"] * 50
    parts[1] = "贵州茅台"
    parts[2] = "600519"
    parts[3] = "1309.30"   # 现价
    parts[4] = "1316.01"   # 昨收
    parts[5] = "1310.00"   # 今开
    parts[6] = "17534"     # 成交量(手)
    parts[7] = "9000"      # 外盘
    parts[8] = "8534"      # 内盘
    parts[30] = "20260908161403"
    parts[31] = "-6.71"    # 涨跌额
    parts[32] = "-0.51"    # 涨跌幅
    parts[33] = "1322.00"  # 最高
    parts[34] = "1300.11"  # 最低
    parts[35] = "1309.30/17534/2302823753"
    parts[36] = "17534"
    parts[37] = "230282"
    for idx, val in (overrides or {}).items():
        parts[idx] = val
    return f'v_sh600519="{"~".join(parts)}";'


class TestTencentMissingFields:
    def test_full_line_is_ok(self):
        q = _parse_line(_tencent_line(), "CN")
        assert q is not None
        assert q.current_price == 1309.30
        assert q.status == "ok"
        assert q.missing_fields == []

    def test_missing_price_is_none_not_zero(self):
        """验收: 字段残缺报文 → current_price is None, 不是 0.0。"""
        q = _parse_line(_tencent_line({3: ""}), "CN")
        assert q is not None
        assert q.current_price is None
        assert q.status in ("partial", "missing")
        assert "current_price" in q.missing_fields

    def test_all_price_fields_missing_is_missing_status(self):
        q = _parse_line(
            _tencent_line({3: "", 4: "", 5: "", 33: "", 34: ""}), "CN"
        )
        assert q is not None
        assert q.current_price is None
        assert q.status == "missing"
        assert set(q.missing_fields) >= {"current_price", "prev_close", "open_price", "high_price", "low_price"}

    def test_partial_missing_volume_keeps_price(self):
        q = _parse_line(_tencent_line({6: "", 32: ""}), "CN")
        assert q is not None
        assert q.current_price == 1309.30
        assert q.status == "partial"
        assert "volume" in q.missing_fields
        assert "change_pct" in q.missing_fields
        assert q.change_pct is None  # 不是 0(0% 是真实值, 缺失是无数据)

    def test_turnover_missing_is_none(self):
        q = _parse_line(_tencent_line({35: "1309.30/17534/"}), "CN")
        assert q is not None
        assert q.turnover is None
        assert "turnover" in q.missing_fields

    def test_turnover_unit_is_yuan(self):
        """单位实测(2026-09-08 收盘后真抓): parts[35] 第三段=成交额(元)。

        证据: 茅台 amt/(price×vol(手)×100)=1.0031, 平安=1.0069 (≈1, VWAP 偏差内);
        parts[37] 恒等于 amt/10000 (万元口径), 交叉印证。
        """
        q = _parse_line(_tencent_line(), "CN")
        assert q is not None
        # fixture 数据对齐实抓报文: 2302823753 / (1309.30×17534×100) ≈ 1.003
        ratio = q.turnover / (q.current_price * q.volume * 100)
        assert 0.99 < ratio < 1.01
        assert q.turnover > 1e8  # 亿元级大额, 若单位是万元该值会 <1e4

    def test_price_zero_still_zero(self):
        """真实 0 值(如未开盘)与缺失的区分: "0.00" 是合法解析值, 不是缺失。"""
        q = _parse_line(_tencent_line({3: "0.00"}), "CN")
        assert q is not None
        assert q.current_price == 0.0
        assert "current_price" not in q.missing_fields


class TestZhituMissingFields:
    def test_num_helper_preserves_none(self):
        assert _num({"a": "1.5"}, "a") == 1.5
        assert _num({"a": ""}, "a", "b") is None
        assert _num({}, "x", "y") is None
        assert _num({"b": "2"}, "a", "b") == 2.0

    def test_capital_flow_missing_keys_are_none(self, monkeypatch):
        import marketdata.vendors.zhitu_full as zf

        monkeypatch.setattr(zf, "zhitu_capital_flow", lambda code, latest: [{"main_net": "100"}])
        from marketdata.symbol import Symbol
        sym = Symbol.parse("600519", "CN")
        out = ZhituCapitalFlowVendor().fetch([sym], {})
        assert len(out) == 1
        assert out[0].main_net_inflow == 100.0
        assert out[0].super_net_inflow is None  # 不是 0
        assert out[0].big_net_inflow is None

    def test_kline_drops_rows_without_ohlc(self, monkeypatch):
        import marketdata.vendors.zhitu_full as zf

        rows = [
            {"date": "2026-09-08", "open": "1", "high": "2", "low": "0.5", "close": "1.5", "volume": "10"},
            {"date": "2026-09-07", "open": "", "high": "2", "low": "0.5", "close": "1.5", "volume": "10"},
        ]
        monkeypatch.setattr(zf, "zhitu_kline", lambda code, level, latest: rows)
        from marketdata.symbol import Symbol
        sym = Symbol.parse("600519", "CN")
        out = ZhituKlineVendor().fetch([sym], {})
        # 缺 OHLC 的行丢弃, 绝不产出 0 价格 bar 混进均线
        assert len(out) == 1
        assert out[0].date == "2026-09-08"


class TestUSVendorsMissingFields:
    def test_alphavantage_missing_fields_are_none(self):
        from marketdata.symbol import Symbol
        from marketdata.vendors.alphavantage import AlphaVantageQuoteVendor

        import json
        raw = json.dumps({"Global Quote": {"01. symbol": "AAPL", "05. price": "150.5"}})
        q = AlphaVantageQuoteVendor._parse(raw, Symbol.parse("AAPL", "US"))
        assert q is not None
        assert q.current_price == 150.5
        assert q.prev_close is None  # 不是 0
        assert q.volume is None
        assert q.status == "partial"
        assert "prev_close" in q.missing_fields

    def test_alphavantage_all_missing_is_missing(self):
        from marketdata.symbol import Symbol
        from marketdata.vendors.alphavantage import AlphaVantageQuoteVendor

        import json
        raw = json.dumps({"Global Quote": {"01. symbol": "AAPL"}})
        q = AlphaVantageQuoteVendor._parse(raw, Symbol.parse("AAPL", "US"))
        assert q is not None
        assert q.current_price is None
        assert q.status == "missing"

    def test_twelvedata_missing_fields_are_none(self):
        from marketdata.symbol import Symbol
        from marketdata.vendors.twelvedata import TwelveDataQuoteVendor

        import json
        raw = json.dumps({"symbol": "AAPL", "close": "150.5", "name": "Apple"})
        q = TwelveDataQuoteVendor._parse(raw, Symbol.parse("AAPL", "US"))
        assert q is not None
        assert q.current_price == 150.5
        assert q.high_price is None
        assert q.status == "partial"


class TestQuoteDataclass:
    def test_quote_defaults(self):
        q = Quote(symbol="600519", market="CN")
        assert q.current_price is None
        assert q.status == "ok"
        assert q.missing_fields == []


class TestMdQuoteRowsPassthrough:
    def test_quote_to_row_carries_status(self):
        from src.core.marketdata_client import _quote_to_row

        q = Quote(symbol="600519", market="CN", current_price=None,
                  status="partial", missing_fields=["current_price"])
        row = _quote_to_row(q)
        assert row["status"] == "partial"
        assert row["missing_fields"] == ["current_price"]
        assert row["current_price"] is None

    def test_md_stock_data_skips_priceless_quotes(self, monkeypatch):
        """缺价 Quote 在 md_stock_data 被跳过(=调用方视角的「无数据」), 绝不变 0.0。"""
        import src.core.marketdata_client as mc

        good = Quote(symbol="600519", market="CN", current_price=100.0, status="ok")
        bad = Quote(symbol="000001", market="CN", current_price=None,
                    status="missing", missing_fields=["current_price"])
        partial = Quote(symbol="600036", market="CN", current_price=30.0,
                        status="partial", missing_fields=["volume"])

        class _MD:
            def quotes(self, syms, market="CN"):
                return [good, bad, partial]

        monkeypatch.setattr(mc, "get_market_data", lambda: _MD())
        out = mc.md_stock_data(["600519", "000001", "600036"], "CN")
        symbols = [s.symbol for s in out]
        assert "000001" not in symbols          # 缺价 → 无数据, 不进 StockData
        assert [s.symbol for s in out] == ["600519", "600036"]
        assert out[1].status == "partial"       # status 随行透传
