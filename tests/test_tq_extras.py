"""TQ 清单切换: 纯解析器单测(不连网关, CI 可跑)。

覆盖 marketdata.vendors.tq 新增的 gp_pairs / lhb_rows / breadth / exday_latest。
真实 payload 形状取自 2026-09-23 对 172.27.16.1:17709 的实测。
"""
from __future__ import annotations

import pytest

from marketdata.vendors.tq import (
    LHB_TABLES,
    breadth,
    exday_latest,
    gp_pairs,
    lhb_rows,
)

# ── 实测 payload 片段 ────────────────────────────────────────────────
_RAW = {
    "GP02": [
        {"Date": "20251219", "Value": ["14881.66", "23043.70"]},
        {"Date": "20251222", "Value": ["19119.34", "16286.94"]},
        {"Date": "20251229", "Value": ["1443198.50", "1443198.50"]},
    ],
    "GP08": [{"Date": "20251222", "Value": ["2.00", "6810.70"]}],
    "GP09": [{"Date": "20251219", "Value": ["2.00", "6029.40"]}],
    "GP17": [
        {"Date": "20251219", "Value": ["6452.20", "17082.80"]},
        {"Date": "20251222", "Value": ["12674.40", "6815.60"]},
        {"Date": "20251229", "Value": ["1421964.25", "1425465.50"]},
    ],
    "GP18": [{"Date": "20251229", "Value": ["21234.23", "17733.06"]}],
}


class TestGpPairs:
    def test_归并成日期宽表(self):
        p = gp_pairs(_RAW)
        assert set(p) == {"20251219", "20251222", "20251229"}
        assert p["20251219"]["GP02"] == [14881.66, 23043.70]

    def test_非dict输入回空(self):
        assert gp_pairs(None) == {}
        assert gp_pairs([1, 2]) == {}

    def test_跳过缺Date或Value的行(self):
        p = gp_pairs({"GP02": [{"Date": "", "Value": ["1"]}, {"Value": ["2"]},
                               {"Date": "20250101", "Value": "不是列表"},
                               {"Date": "20250102", "Value": ["3"]}]})
        assert p == {"20250102": {"GP02": [3.0]}}

    def test_表值非列表时忽略该表(self):
        assert gp_pairs({"GP02": "坏数据"}) == {}


class TestLhbRows:
    def test_字段映射(self):
        rows = {r["date"]: r for r in lhb_rows(gp_pairs(_RAW))}
        r = rows["20251219"]
        assert r["buy"] == 14881.66 and r["sell"] == 23043.70
        # GP09 = [机构个数, 金额] —— 顺序与 GP17 不同
        assert r["inst_buy_cnt"] == 2.0 and r["inst_buy_amount"] == 6029.40
        assert r["yyb_buy"] == 6452.20 and r["yyb_sell"] == 17082.80
        # GP08 该日无值 → None, 不编造
        assert r["inst_sell_cnt"] is None and r["inst_sell_amount"] is None

    def test_GP08映射到卖方(self):
        rows = {r["date"]: r for r in lhb_rows(gp_pairs(_RAW))}
        r = rows["20251222"]
        assert r["inst_sell_cnt"] == 2.0 and r["inst_sell_amount"] == 6810.70
        assert r["inst_buy_amount"] is None

    def test_升序返回(self):
        dates = [r["date"] for r in lhb_rows(gp_pairs(_RAW))]
        assert dates == sorted(dates)

    def test_交叉核对仅对该样本日成立(self):
        """GP02 与 GP17+GP18 的关系是**近似**不是恒等式:
        20251229 精确相等, 但实测 20260819 差 +4629.77 万 → 不能当校验断言。
        这里只断言"该样本日恰好相符", 不宣称普遍规律。"""
        rows = {r["date"]: r for r in lhb_rows(gp_pairs(_RAW))}
        r = rows["20251229"]
        assert abs(r["buy"] - (r["yyb_buy"] + r["hsgt_buy"])) < 1.0
        assert abs(r["sell"] - (r["yyb_sell"] + r["hsgt_sell"])) < 1.0

    def test_异常签名被标记(self):
        """buy==sell 且量级远超中位数 → suspicious(20251229 实测 144 亿)。"""
        rows = {r["date"]: r for r in lhb_rows(gp_pairs(_RAW))}
        assert rows["20251229"]["suspicious"] is True
        assert rows["20251219"]["suspicious"] is False
        assert rows["20251222"]["suspicious"] is False

    def test_异常行不丢弃(self):
        """只标记不丢 —— 口径归 TQ, 消费方决定。"""
        assert len(lhb_rows(gp_pairs(_RAW))) == 3

    def test_全空日期被跳过(self):
        rows = lhb_rows({"20250101": {"GP02": None, "GP08": None}})
        assert rows == []

    def test_短数组不抛异常(self):
        rows = lhb_rows({"20250101": {"GP02": [1.0]}})
        assert rows[0]["buy"] == 1.0 and rows[0]["sell"] is None

    def test_LHB_TABLES常量(self):
        assert LHB_TABLES == ("GP02", "GP08", "GP09", "GP17", "GP18")


class TestBreadth:
    def test_涨跌平统计(self, monkeypatch):
        fake = {"600519.SH": {"Zaf": "1.5"}, "000001.SZ": {"Zaf": "-2.0"},
                "002361.SZ": {"Zaf": "0"}, "300750.SZ": {"Zaf": "-0.1"}}

        def _rpc(m, p, timeout=None):
            return fake

        out = breadth(list(fake), _rpc_fn=_rpc)
        assert (out["up"], out["down"], out["flat"]) == (1, 2, 1)
        assert out["total"] == 4 and out["failed_chunks"] == 0

    def test_缺Zaf的票不计入(self):
        def _rpc(m, p, timeout=None):
            return {"A": {"Zaf": "1"}, "B": {}, "C": {"Zaf": None}}

        out = breadth(["A", "B", "C"], _rpc_fn=_rpc)
        assert out["up"] == 1 and out["total"] == 1 and out["codes_ok"] == 1

    def test_分片按chunk切(self):
        seen = []

        def _rpc(m, p, timeout=None):
            seen.append(len(p["stock_list"]))
            return {}

        breadth([f"{i:06d}.SZ" for i in range(11)], chunk=5, _rpc_fn=_rpc)
        assert seen == [5, 5, 1]

    def test_单片失败被计数不致命(self):
        calls = {"n": 0}

        def _rpc(m, p, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("客户端假死")
            return {"X": {"Zaf": "1"}}

        out = breadth(["A", "B", "C", "D"], chunk=2, _rpc_fn=_rpc)
        assert out["failed_chunks"] == 1 and out["up"] == 1

    def test_空输入(self):
        assert breadth([])["total"] == 0


class TestExdayLatest:
    def test_撤单量解析(self):
        payload = [{"BCancel": "194573.00", "SCancel": "8871.00", "BOrder": "65",
                    "SOrder": "9", "TotalBOrder": "1234", "TotalSOrder": "567",
                    "CJBS": "48213", "VolNum": "436919",
                    "Amo": [[1, 2, 3, 4]] * 4, "Vol": [[1, 2, 3, 4]] * 4,
                    "Date": "20260923"}]

        def _rpc(m, p, timeout=None):
            assert p["stock_code"] == "002361.SZ", "网关认 stock_code, 不是 codestr"
            return payload

        out = exday_latest("002361.SZ", _rpc_fn=_rpc)
        assert out["b_cancel"] == 194573.0 and out["s_cancel"] == 8871.0
        assert out["total_b_order"] == 1234.0 and out["trades"] == 48213.0
        assert out["date"] == "20260923" and len(out["Amo"]) == 4

    def test_取最后一条(self):
        payload = [{"BCancel": "1", "Date": "d1"}, {"BCancel": "2", "Date": "d2"}]
        assert exday_latest("X", _rpc_fn=lambda m, p, timeout=None: payload)["b_cancel"] == 2.0

    def test_空返回不抛(self):
        assert exday_latest("X", _rpc_fn=lambda m, p, timeout=None: []) == {}
        assert exday_latest("X", _rpc_fn=lambda m, p, timeout=None: None) == {}

    def test_Value包裹也能解析(self):
        def _rpc(m, p, timeout=None):
            return {"Value": [{"BCancel": "5.00", "Date": "d"}]}

        assert exday_latest("X", _rpc_fn=_rpc)["b_cancel"] == 5.0
