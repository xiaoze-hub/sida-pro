"""股东户数/分红: 跨源口径 + Symbol 后缀归一化 (2026-09-24)。

背景(全部实测标定, 非推测):
  · 生产上"股东户数/分红"**恒为空** —— 智兔股东接口 429; 东财侧 filter 拿的是
    带交易所后缀的 code(`Symbol.parse` 只 strip 不归一化), SECURITY_CODE=
    "002361.SZ" 恒 0 条而裸码 3 条。
  · 东财分红 PRETAX_BONUS_RMB 是**每10股**口径, 被当成"每股"用 → 差 10 倍。
  · TQ 侧数据可用且与东财逐笔对齐 → 接为备源。

CI 无 TQ 网关/无外网, 全部走 monkeypatch 假源。
"""

from __future__ import annotations

import pytest

import marketdata.vendors.tq as tqmod
from marketdata.symbol import Market, Symbol
from marketdata.vendors.market_flow import (
    EastmoneyDividendVendor,
    EastmoneyShareholdersVendor,
)
from marketdata.vendors.tq import (
    TqDividendVendor,
    TqShareholdersVendor,
    _fmt_day,
    divid_factors_rows,
)


def _cn(code: str = "002361") -> Symbol:
    return Symbol.parse(code, "CN")


# --------------------------------------------------------------------------- #
# 1) Symbol 后缀归一化: 一切下游(filter / to_tq_code len==6)都靠裸码
# --------------------------------------------------------------------------- #


class TestSymbolSuffix:
    @pytest.mark.parametrize(
        "raw,expect_market,expect_code",
        [
            ("002361.SZ", Market.CN, "002361"),
            ("002361.sz", Market.CN, "002361"),
            ("600519.SH", Market.CN, "600519"),
            ("430047.BJ", Market.CN, "430047"),
            ("002361", Market.CN, "002361"),
            ("00700.HK", Market.HK, "00700"),
            ("00700", Market.HK, "00700"),
            ("AAPL", Market.US, "AAPL"),
        ],
    )
    def test_带后缀归一化为裸码且市场正确(self, raw, expect_market, expect_code):
        s = Symbol.parse(raw)
        assert s.code == expect_code
        assert s.market == expect_market

    def test_美股点号不被当交易所后缀剥掉(self):
        """BRK.B 的 ".B" 是股票类别不是交易所 —— 贪心剥点会毁掉美股代码。"""
        s = Symbol.parse("BRK.B")
        assert s.code == "BRK.B"
        assert s.market == Market.US

    def test_显式market时不覆盖(self):
        s = Symbol.parse("002361.SZ", "CN")
        assert (s.market, s.code) == (Market.CN, "002361")

    def test_归一化后腾讯与东财格式正确(self):
        """回归: 未归一化时 to_tencent 会产出 'sz002361.SZ'(坏)。"""
        s = Symbol.parse("002361.SZ")
        assert s.to_tencent() == "sz002361"
        assert s.to_eastmoney_secid() == "0.002361"

    @pytest.mark.parametrize("raw", ["002361.SZ", "600519.SH", "430047.BJ", "920819.BJ"])
    def test_带后缀的代码也能过TQ的6位门禁(self, raw):
        """to_tq_code 要求 len(code)==6; 归一化前带后缀的入参一律返回 None。"""
        from marketdata.vendors.tq import to_tq_code

        got = to_tq_code(Symbol.parse(raw))
        assert got is not None and got.endswith((".SZ", ".SH", ".BJ"))


# --------------------------------------------------------------------------- #
# 2) 日期格式跨源一致(API 按 ex_date 字符串倒序排, 混格式会乱序)
# --------------------------------------------------------------------------- #


class TestFmtDay:
    @pytest.mark.parametrize(
        "raw,expect",
        [("20260630", "2026-06-30"), ("2026-06-30 00:00:00", "2026-06-30"),
         ("", ""), (None, "")],
    )
    def test_格式(self, raw, expect):
        assert _fmt_day(raw) == expect

    def test_与东财路径同形(self):
        """东财 ex_date 是 str(...)[:10] 即 YYYY-MM-DD; TQ 必须同形。"""
        assert len(_fmt_day("20110530")) == 10 and _fmt_day("20110530")[4] == "-"


# --------------------------------------------------------------------------- #
# 3) TQ 股东户数(GP01): 最新一期 + 环比现算
# --------------------------------------------------------------------------- #


def _fake_rpc(method, params, timeout=None, **kw):
    if method == "get_gpjy_value":
        assert params["table_list"] == ["GP01"]
        return {
            "GP01": [
                {"Date": "20260331", "Value": ["190899.00", "0.00"]},
                {"Date": "20260630", "Value": ["264938.00", "0.00"]},
            ]
        }
    if method == "get_divid_factors":
        # 契约: 必须 full=True 才拿得到 Date 数组(原包装只取 Value 会丢日期)
        assert kw.get("full") is True, "get_divid_factors 必须带 full=True"
        return {
            "Date": ["20110530", "20130416", "20250721"],
            "Type": ["1", "1", "1"],
            "Value": [
                ["1.50", "0.00", "10.00", "0.00"],   # 10转10派1.50元
                ["2.00", "0.00", "10.00", "0.00"],   # 10送2转8派2.00元
                ["0.50", "0.00", "0.00", "0.00"],    # 10派0.50元
            ],
        }
    raise AssertionError(f"未预料的 TQ 方法: {method}")


class TestTqShareholdersVendor:
    def test_取最新一期并算环比(self, monkeypatch):
        monkeypatch.setattr(tqmod, "_rpc", _fake_rpc)
        rows = TqShareholdersVendor().fetch([_cn()], {})
        assert len(rows) == 1
        r = rows[0]
        assert r.report_date == "2026-06-30"      # 与东财 END_DATE 同形
        assert r.holder_num == 264938             # 与东财 HOLDER_NUM 实测一致
        assert r.change_num == 264938 - 190899    # 环比由前一期现算
        assert r.change_ratio == pytest.approx(38.79, abs=0.01)
        assert r.avg_shares is None               # GP01 无户均持股, 不臆造

    def test_只一期时环比为None(self, monkeypatch):
        def _one(method, params, timeout=None, **kw):
            return {"GP01": [{"Date": "20260630", "Value": ["264938.00", "0.00"]}]}

        monkeypatch.setattr(tqmod, "_rpc", _one)
        r = TqShareholdersVendor().fetch([_cn()], {})[0]
        assert r.change_num is None and r.change_ratio is None

    def test_坏行被跳过不炸批(self, monkeypatch):
        def _bad(method, params, timeout=None, **kw):
            return {
                "GP01": [
                    {"Date": "20260331", "Value": []},          # 空 Value
                    "not-a-dict",                                # 脏行
                    {"Date": "20260630", "Value": ["abc"]},      # 非数值
                    {"Date": "20260630", "Value": ["264938.00"]},
                ]
            }

        monkeypatch.setattr(tqmod, "_rpc", _bad)
        rows = TqShareholdersVendor().fetch([_cn()], {})
        assert len(rows) == 1 and rows[0].holder_num == 264938

    def test_网关异常时返回空交给下一源(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("TQ 隧道断开")

        monkeypatch.setattr(tqmod, "_rpc", _boom)
        assert TqShareholdersVendor().fetch([_cn()], {}) == []

    def test_非CN市场不处理(self, monkeypatch):
        monkeypatch.setattr(tqmod, "_rpc", _fake_rpc)
        assert TqShareholdersVendor().fetch([Symbol.parse("AAPL")], {}) == []


# --------------------------------------------------------------------------- #
# 4) TQ 分红: 每10股派息 → 每股(/10); 送股转增合并
# --------------------------------------------------------------------------- #


class TestTqDividendVendor:
    def test_日期保留且派息除10(self, monkeypatch):
        monkeypatch.setattr(tqmod, "_rpc", _fake_rpc)
        rows = TqDividendVendor().fetch([_cn()], {})
        assert [r.ex_date for r in rows] == ["2011-05-30", "2013-04-16", "2025-07-21"]
        # Bonus 每10股 → 每股: 1.50/10, 2.00/10, 0.50/10
        assert [r.dividend_per_share for r in rows] == [0.15, 0.2, 0.05]

    def test_送转合计记入bonus_ratio且transfer留空(self, monkeypatch):
        """TQ 不区分送股/转增(实测: 送0转10 与 送2转8 都回 10.00)。"""
        monkeypatch.setattr(tqmod, "_rpc", _fake_rpc)
        rows = TqDividendVendor().fetch([_cn()], {})
        assert rows[0].bonus_ratio == 10.0
        assert rows[1].bonus_ratio == 10.0
        assert rows[2].bonus_ratio is None
        assert all(r.transfer_ratio is None for r in rows)

    def test_无方案进度不臆造(self, monkeypatch):
        monkeypatch.setattr(tqmod, "_rpc", _fake_rpc)
        assert all(r.progress == "" for r in TqDividendVendor().fetch([_cn()], {}))

    def test_带后缀的symbol也能取数(self, monkeypatch):
        """回归项: 端点传进来的是 URL 原文 '002361.SZ'。"""
        monkeypatch.setattr(tqmod, "_rpc", _fake_rpc)
        rows = TqDividendVendor().fetch([Symbol.parse("002361.SZ", "CN")], {})
        assert len(rows) == 3

    def test_网关异常时返回空(self, monkeypatch):
        def _boom(*a, **kw):
            raise RuntimeError("TQ 隧道断开")

        monkeypatch.setattr(tqmod, "_rpc", _boom)
        assert TqDividendVendor().fetch([_cn()], {}) == []

    def test_并列数组长度不齐时按短边截断(self, monkeypatch):
        def _uneven(method, params, timeout=None, **kw):
            return {"Date": ["20110530", "20130416"], "Value": [["1.50", "0", "10.00", "0"]]}

        monkeypatch.setattr(tqmod, "_rpc", _uneven)
        assert len(divid_factors_rows("002361.SZ")) == 1

    def test_非dict响应返回空(self, monkeypatch):
        monkeypatch.setattr(tqmod, "_rpc", lambda *a, **kw: ["1.50", "0", "10", "0"])
        assert divid_factors_rows("002361.SZ") == []


# --------------------------------------------------------------------------- #
# 5) 东财侧: filter 用裸码 + 每10股派息除10
# --------------------------------------------------------------------------- #


class TestEastmoneyFixes:
    def test_股东户数filter用裸码(self, monkeypatch):
        seen: list[str] = []

        def _cap(report, filter_str, sort_col, *, page_size=1):
            seen.append(filter_str)
            return [{"END_DATE": "2026-06-30 00:00:00", "HOLDER_NUM": 264938}]

        monkeypatch.setattr("marketdata.vendors.market_flow._datacenter_get", _cap)
        rows = EastmoneyShareholdersVendor().fetch([Symbol.parse("002361.SZ", "CN")], {})
        assert seen == ['(SECURITY_CODE="002361")']   # 不是 "002361.SZ"
        assert rows[0].report_date == "2026-06-30"

    def test_分红filter用裸码且每10股派息除10(self, monkeypatch):
        seen: list[str] = []

        def _cap(report, filter_str, sort_col, *, page_size=1):
            seen.append(filter_str)
            return [
                {"EX_DIVIDEND_DATE": "2025-07-21 00:00:00", "PRETAX_BONUS_RMB": 0.5,
                 "IT_RATIO": None, "BONUS_RATIO": None, "ASSIGN_PROGRESS": "实施分配"},
                {"EX_DIVIDEND_DATE": "2013-04-16 00:00:00", "PRETAX_BONUS_RMB": 2.0,
                 "IT_RATIO": 8.0, "BONUS_RATIO": 2.0, "ASSIGN_PROGRESS": "实施分配"},
            ]

        monkeypatch.setattr("marketdata.vendors.market_flow._datacenter_get", _cap)
        rows = EastmoneyDividendVendor().fetch([Symbol.parse("002361.SZ", "CN")], {})
        assert seen == ['(SECURITY_CODE="002361")']
        # 0.5 元/10股 → 0.05 元/股(此前直接透传, 前端按"元/股"显示 → 差 10 倍)
        assert [r.dividend_per_share for r in rows] == [0.05, 0.2]
        # 送/转仍分开(TQ 备源做不到, 主源保持更细口径)
        assert rows[1].transfer_ratio == 8.0 and rows[1].bonus_ratio == 2.0

    def test_转增取IT_RATIO字段(self, monkeypatch):
        """回归: 该报表**没有** TRANSFER_RATIO 键, 原来取它 → 转增恒为 None。

        实测对齐: 2013-04-16 BONUS_RATIO=2 / IT_RATIO=8 ↔ 方案"10送2.00转8.00"。
        """

        def _cap(report, filter_str, sort_col, *, page_size=1):
            row = {"EX_DIVIDEND_DATE": "2013-04-16 00:00:00", "PRETAX_BONUS_RMB": 2.0,
                   "IT_RATIO": 8.0, "BONUS_RATIO": 2.0, "ASSIGN_PROGRESS": "实施分配"}
            assert "TRANSFER_RATIO" not in row, "报表无此键, 留作反例"
            return [row]

        monkeypatch.setattr("marketdata.vendors.market_flow._datacenter_get", _cap)
        r = EastmoneyDividendVendor().fetch([_cn()], {})[0]
        assert r.transfer_ratio == 8.0
        assert r.bonus_ratio == 2.0

    def test_派息为0或空时留None(self, monkeypatch):
        def _cap(report, filter_str, sort_col, *, page_size=1):
            return [{"EX_DIVIDEND_DATE": "2025-07-21 00:00:00", "PRETAX_BONUS_RMB": None}]

        monkeypatch.setattr("marketdata.vendors.market_flow._datacenter_get", _cap)
        rows = EastmoneyDividendVendor().fetch([_cn()], {})
        assert rows[0].dividend_per_share is None
