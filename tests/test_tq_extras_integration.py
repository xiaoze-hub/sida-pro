"""清单切换集成测: 涨跌家数(TQ pricevol) + 龙虎榜(TQ GP 序列融合) + 撤单量。

都走 monkeypatch 假源 —— CI 无 TQ 网关, 必须离线可测。
"""
from __future__ import annotations

import pytest

from src.core import tdx_boards


# ═══════════════ 涨跌家数 (tdx_boards.market_breadth) ═══════════════
@pytest.fixture(autouse=True)
def _clear_tdx_caches():
    tdx_boards._clear_caches()
    yield
    tdx_boards._clear_caches()


class TestMarketBreadth:
    def _patch(self, monkeypatch, pv_map, codes=None):
        codes = codes or list(pv_map)
        monkeypatch.setattr(tdx_boards, "name_map", lambda: {c: c for c in codes})
        monkeypatch.setattr(tdx_boards, "_rpc", lambda m, p, timeout=6.0: pv_map)

    def test_涨跌平计数(self, monkeypatch):
        self._patch(monkeypatch, {"A": {"Zaf": "3.1"}, "B": {"Zaf": "-1.2"},
                                  "C": {"Zaf": "0"}, "D": {"Zaf": "0.5"}})
        out = tdx_boards.market_breadth()
        assert (out["up"], out["down"], out["flat"]) == (2, 1, 1)
        assert out["total"] == 4 and out["source"] == "tdx_tq"

    def test_缺Zaf不计入任何档(self, monkeypatch):
        """诚实口径: 无涨跌幅的票不塞进任何一档(不假装平盘)。"""
        self._patch(monkeypatch, {"A": {"Zaf": "1"}, "B": {}, "C": {"Zaf": None}})
        out = tdx_boards.market_breadth()
        assert out["total"] == 1 and out["codes_ok"] == 1 and out["flat"] == 0

    def test_全A代码为空时降级None(self, monkeypatch):
        monkeypatch.setattr(tdx_boards, "name_map", lambda: {})
        assert tdx_boards.market_breadth() is None

    def test_全片无有效数据时降级None(self, monkeypatch):
        """不能返回全 0 —— 那看起来像"全市场平盘", 是编造。"""
        self._patch(monkeypatch, {"A": {}, "B": {}})
        assert tdx_boards.market_breadth() is None

    def test_单片失败被计数且其余照算(self, monkeypatch):
        # 必须让代码真的切出多片: _BATCH 是 500, 所以把批大小压到 3 来构造两片
        monkeypatch.setattr(tdx_boards, "_BATCH", 3)
        monkeypatch.setattr(tdx_boards, "name_map", lambda: {f"C{i}": f"C{i}" for i in range(6)})
        calls = {"n": 0}

        def _rpc(m, p, timeout=6.0):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("客户端连接被重置")
            return {c: {"Zaf": "1.0"} for c in p["stock_list"]}

        monkeypatch.setattr(tdx_boards, "_rpc", _rpc)
        out = tdx_boards.market_breadth()
        assert out["failed_chunks"] == 1 and out["up"] == 3 and out["total"] == 3

    def test_分片不超过_BATCH(self, monkeypatch):
        """事故铁律: 绝不单次发全市场(曾压死客户端)。"""
        seen = []
        codes = [f"{i:06d}.SZ" for i in range(1200)]
        monkeypatch.setattr(tdx_boards, "name_map", lambda: {c: c for c in codes})

        def _rpc(m, p, timeout=6.0):
            seen.append(len(p["stock_list"]))
            return {}

        monkeypatch.setattr(tdx_boards, "_rpc", _rpc)
        tdx_boards.market_breadth()
        assert max(seen) <= tdx_boards._BATCH

    def test_60秒缓存生效(self, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(tdx_boards, "name_map", lambda: {"A": "A"})

        def _rpc(m, p, timeout=6.0):
            calls["n"] += 1
            return {"A": {"Zaf": "1"}}

        monkeypatch.setattr(tdx_boards, "_rpc", _rpc)
        tdx_boards.market_breadth()
        tdx_boards.market_breadth()
        assert calls["n"] == 1

    def test_force跳过缓存(self, monkeypatch):
        calls = {"n": 0}
        monkeypatch.setattr(tdx_boards, "name_map", lambda: {"A": "A"})

        def _rpc(m, p, timeout=6.0):
            calls["n"] += 1
            return {"A": {"Zaf": "1"}}

        monkeypatch.setattr(tdx_boards, "_rpc", _rpc)
        tdx_boards.market_breadth()
        tdx_boards.market_breadth(force=True)
        assert calls["n"] == 2


# ═══════════════ 龙虎榜 TQ 融合 ═══════════════
class TestLhbFromTq:
    def test_裸代码补后缀(self):
        from src.web.api.market_data import _sym_to_tq_code

        assert _sym_to_tq_code("002361") == "002361.SZ"
        assert _sym_to_tq_code("600519") == "600519.SH"
        assert _sym_to_tq_code("920001") == "920001.BJ"   # 92 前缀先判 BJ
        assert _sym_to_tq_code("002361.sz") == "002361.SZ"
        assert _sym_to_tq_code("600519.SH") == "600519.SH"

    def test_万元转元对齐东财口径(self, monkeypatch):
        from src.web.api import market_data as mdmod

        monkeypatch.setattr(
            "marketdata.vendors.tq.lhb_series",
            lambda code, start_time="": [
                {"date": "20251219", "buy": 14881.66, "sell": 23043.70,
                 "inst_buy_amount": 6029.40, "inst_sell_amount": None,
                 "yyb_buy": 6452.20, "yyb_sell": 17082.80,
                 "hsgt_buy": None, "hsgt_sell": None, "suspicious": False},
            ],
        )
        rows = mdmod._lhb_from_tq("002361")
        r = rows[0]
        # 东财 buy_amt 单位=元, TQ=万元 → ×1e4
        assert r["buy_amt"] == pytest.approx(148816600.0)
        assert r["sell_amt"] == pytest.approx(230437000.0)
        assert r["net_buy"] == pytest.approx(-81620400.0)
        assert r["trade_date"] == "20251219"

    def test_东财独占字段留空不编造(self, monkeypatch):
        from src.web.api import market_data as mdmod

        monkeypatch.setattr(
            "marketdata.vendors.tq.lhb_series",
            lambda code, start_time="": [
                {"date": "20251219", "buy": 1.0, "sell": 2.0, "inst_buy_amount": None,
                 "inst_sell_amount": None, "yyb_buy": None, "yyb_sell": None,
                 "hsgt_buy": None, "hsgt_sell": None, "suspicious": False}],
        )
        r = mdmod._lhb_from_tq("002361")[0]
        assert r["reason"] is None and r["close"] is None and r["top_buyers"] == []

    def test_空序列返回空(self, monkeypatch):
        from src.web.api import market_data as mdmod

        monkeypatch.setattr("marketdata.vendors.tq.lhb_series",
                            lambda code, start_time="": [])
        assert mdmod._lhb_from_tq("002361") == []


class TestFundamentalsMerge:
    """TQ 骨架 + 东财补 reason —— 合并后应比单源都全。"""

    def _run(self, monkeypatch, tq_rows, em_by_date, expect_pending=False):
        from src.web.api import market_data as mdmod

        monkeypatch.setattr(mdmod, "_get_lhb_range",
                            lambda market, days: {"by_date": em_by_date} if em_by_date is not None else None)
        monkeypatch.setattr("marketdata.vendors.tq.lhb_series",
                            lambda code, start_time="": tq_rows)

        class _MD:
            def margin(self, *a, **k):
                return []

            def shareholders(self, *a, **k):
                return []

            def dividend(self, *a, **k):
                return []

            def events(self, *a, **k):
                return []

        monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD())
        return mdmod.fetch_fundamentals_detail("002361", market="CN", dt_days=10)

    def test_TQ与东财合并_补上reason(self, monkeypatch):
        tq = [{"date": "20251219", "buy": 100.0, "sell": 200.0, "inst_buy_amount": None,
               "inst_sell_amount": None, "yyb_buy": None, "yyb_sell": None,
               "hsgt_buy": None, "hsgt_sell": None, "suspicious": False}]
        em = {"20251219": [{"trade_date": "20251219", "symbol": "002361",
                            "name": "神剑股份", "reason": "日涨幅偏离值达7%",
                            "close": 10.42, "change_pct": 9.98, "turnover_pct": 12.3,
                            "top_buyers": [{"name": "某营业部"}], "top_sellers": []}]}
        out = self._run(monkeypatch, tq, em)
        r = out["dragon_tiger"][0]
        assert r["reason"] == "日涨幅偏离值达7%" and r["name"] == "神剑股份"
        assert r["close"] == 10.42 and r["top_buyers"] == [{"name": "某营业部"}]
        assert out["lhb_source"] == "tdx_tq+em_merge"

    def test_TQ侧金额不被东财覆盖(self, monkeypatch):
        tq = [{"date": "20251219", "buy": 100.0, "sell": 200.0, "inst_buy_amount": None,
               "inst_sell_amount": None, "yyb_buy": None, "yyb_sell": None,
               "hsgt_buy": None, "hsgt_sell": None, "suspicious": False}]
        em = {"20251219": [{"trade_date": "20251219", "symbol": "002361",
                            "buy_amt": 99999.0, "sell_amt": 88888.0, "reason": "x"}]}
        out = self._run(monkeypatch, tq, em)
        assert out["dragon_tiger"][0]["buy_amt"] == pytest.approx(1_000_000.0)

    def test_东财独有日期不丢(self, monkeypatch):
        tq = [{"date": "20250101", "buy": 1.0, "sell": 1.0, "inst_buy_amount": None,
               "inst_sell_amount": None, "yyb_buy": None, "yyb_sell": None,
               "hsgt_buy": None, "hsgt_sell": None, "suspicious": False}]
        em = {"20251219": [{"trade_date": "20251219", "symbol": "002361", "reason": "东财独有"}]}
        out = self._run(monkeypatch, tq, em)
        dates = [r["trade_date"] for r in out["dragon_tiger"]]
        assert "20250101" in dates and "20251219" in dates

    def test_TQ命中后不再pending(self, monkeypatch):
        """TQ 拿到就不等东财后台任务 —— 冷启动不再阻塞。"""
        tq = [{"date": "20250101", "buy": 1.0, "sell": 1.0, "inst_buy_amount": None,
               "inst_sell_amount": None, "yyb_buy": None, "yyb_sell": None,
               "hsgt_buy": None, "hsgt_sell": None, "suspicious": False}]
        out = self._run(monkeypatch, tq, None)
        assert out["lhb_pending"] is False and out["lhb_source"] == "tdx_tq"

    def test_TQ为空时降级东财且pending(self, monkeypatch):
        out = self._run(monkeypatch, [], None)
        assert out["lhb_pending"] is True and out["lhb_source"] == "em"

    def test_倒序新到旧(self, monkeypatch):
        tq = [{"date": d, "buy": 1.0, "sell": 1.0, "inst_buy_amount": None,
               "inst_sell_amount": None, "yyb_buy": None, "yyb_sell": None,
               "hsgt_buy": None, "hsgt_sell": None, "suspicious": False}
              for d in ("20250101", "20251219", "20250601")]
        out = self._run(monkeypatch, tq, {})
        assert [r["trade_date"] for r in out["dragon_tiger"]] == ["20251219", "20250601", "20250101"]

    def test_exday字段存在(self, monkeypatch):
        """撤单量为 TQ 净增字段, 无源时也要给出空 dict(契约稳定)。"""
        out = self._run(monkeypatch, [], {})
        assert "exday" in out
