"""TQ 优先铁律（2026-10-20 用户明确要求："逆向研究的通达信客户端接口，一定要优先使用"）。

覆盖两处：
1. 两市成交额兜底链必须 **TQ 指数快照 → 腾讯指数**，顺序不能反；
2. 全市场成交额/主力净流入的 TQ 汇总采集器：覆盖率不足 80% 时**丢弃整批**（半场数据 =
   谎报成交额），不写库。
"""
from __future__ import annotations

import pytest


def test_amount_fallback_prefers_tq(monkeypatch):
    from src.web.api import market_data as md

    calls: list[str] = []

    def _fake_tq():
        calls.append("tq")
        return 8888.8, "tdx_tq_index"

    class _MD:
        def index_quotes(self, codes):
            calls.append("tencent")
            return [{"turnover": 1e11}, {"turnover": 1e11}]

    monkeypatch.setattr(md, "_tq_index_amount", _fake_tq)
    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD())
    md._AMOUNT_CACHE.clear()

    val, src = md._refresh_amount_fallback()
    assert (val, src) == (8888.8, "tdx_tq_index")
    assert calls == ["tq"], "TQ 可用时不许再去打腾讯"


def test_amount_fallback_uses_tencent_when_tq_down(monkeypatch):
    from src.web.api import market_data as md

    class _MD:
        def index_quotes(self, codes):
            return [{"turnover": 3.9e11}, {"turnover": 4.37e11}]

    monkeypatch.setattr(md, "_tq_index_amount", lambda: (None, "tdx_tq_error"))
    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD())
    md._AMOUNT_CACHE.clear()

    val, src = md._refresh_amount_fallback()
    assert src == "tencent_index"
    assert val == pytest.approx(8270.0)


def test_market_totals_drops_partial_batch(monkeypatch):
    """覆盖率不足 80% → 返回 None（不能把半场成交额当全市场）。"""
    from src.collectors import tq_market_flow as mf
    from src.core import tdx_boards

    monkeypatch.setattr(tdx_boards, "name_map", lambda: {f"6000{i:02d}": f"股{i}" for i in range(10)})
    partial = {f"6000{i:02d}": {"amount": 1e8, "fund_net": 1e7, "change_pct": 1.0} for i in range(3)}
    monkeypatch.setattr(tdx_boards, "board_quotes", lambda codes, with_fund=True: partial)

    assert mf.market_totals() is None


def test_market_totals_sums_when_coverage_ok(monkeypatch):
    from src.collectors import tq_market_flow as mf
    from src.core import tdx_boards

    monkeypatch.setattr(tdx_boards, "name_map", lambda: {f"6000{i:02d}": f"股{i}" for i in range(10)})
    full = {f"6000{i:02d}": {"amount": 1e9, "fund_net": -1e8, "change_pct": 1.0 if i % 2 else -1.0}
            for i in range(10)}
    monkeypatch.setattr(tdx_boards, "board_quotes", lambda codes, with_fund=True: full)

    out = mf.market_totals()
    assert out is not None
    assert out["total_amount"] == pytest.approx(100.0)   # 10 × 1e9 = 1e10 元 = 100 亿
    assert out["main_flow"] == pytest.approx(-10.0)      # 10 × -1e8 = -1e9 元 = -10 亿
    assert out["up"] == 5 and out["down"] == 5
    assert out["source"] == "tdx_tq_amo_supamo"


def test_tq_quote_yields_to_batch_source_for_big_lists():
    """列表页(>5 只)必须让位给批量源 —— TQ 逐票 RPC 会把 39 只跑成 5~17s。"""
    from marketdata.symbol import Symbol
    from marketdata.vendors.tq import TqQuoteVendor

    vendor = TqQuoteVendor()
    syms = [Symbol(code=f"60000{i}", market="CN") for i in range(6)]
    assert vendor.fetch(syms, {}) == [], "6 只以上不该再由 TQ 逐票拉"
    assert TqQuoteVendor.MAX_SYMBOLS_FOR_TQ == 5


def test_lhb_amounts_from_tq_units_and_source(monkeypatch):
    """龙虎榜金额：TQ GP02 是**万元**，必须 ×1e4 转元；来源要能标注。"""
    import marketdata.vendors.tq as tqv
    from src.web.api import market_data as md

    monkeypatch.setattr(
        tqv, "lhb_series",
        lambda code, start_time="", end_time="", **kw: [
            {"date": "2026-10-20", "buy": 12345.67, "sell": 8000.0},
            {"date": "2026-10-19", "buy": 1.0, "sell": 1.0},   # 窗口外的行必须被丢掉
        ],
        raising=False,
    )
    out = md._lhb_amounts_from_tq(["600127"], "20261020")
    row = out["600127"]
    assert row["buy_amt"] == pytest.approx(12345.67 * 1e4)
    assert row["sell_amt"] == pytest.approx(8000.0 * 1e4)
    assert row["net_buy"] == pytest.approx(4345.67 * 1e4)
    assert row["source"] == "tdx_tq_gp02"


def test_lhb_amounts_from_tq_empty_when_no_row(monkeypatch):
    """TQ 当日无该股记录 → 空 dict（调用方回落东财金额，绝不补 0）。"""
    import marketdata.vendors.tq as tqv
    from src.web.api import market_data as md

    monkeypatch.setattr(tqv, "lhb_series", lambda *a, **k: [], raising=False)
    assert md._lhb_amounts_from_tq(["600127"], "20261020") == {}
