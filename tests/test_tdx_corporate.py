"""TQ「异动资金档案」单位与口径测试（2026-10-20）。

这五个字段最容易出的错不是取数失败，而是**单位错 10000 倍且不报错**，
所以每条都用真实样本钉住换算：
- GP04 大宗：`[均价(元), 成交额(万元)]` → 成交额 ×1e4
- GP05 增减持：`[均价(元), 变动股数(股)]` → 不换算
- GP19 质押：**万股**（官方文档写"万元"是错的；002361 6354万股/95103.5万股=6.68% 与 GP20 吻合）
- GP20 质押比例：%
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def stub_gp(monkeypatch):
    """把 tq_rpc 换成按表返回样本的桩。"""
    import marketdata.vendors.tq as tqv

    data = {
        "GP04": [{"Date": "20261015", "Value": ["1312.97", "407.02"]}],
        "GP05": [{"Date": "20251226", "Value": ["1443.14", "1274234"]}],
        "GP06": [{"Date": "20260929", "Value": ["55263680"]},
                 {"Date": "20260930", "Value": ["55300000"]}],
        "GP19": [{"Date": "20250718", "Value": ["6354.00", "0.00"]}],
        "GP20": [{"Date": "20250718", "Value": ["6.68", "0.00"]}],
    }

    def _rpc(method, params, **kw):
        if method == "get_gpjy_value":
            tables = (params or {}).get("table_list") or []
            return {t: data[t] for t in tables if t in data}
        if method == "get_financial_data":
            return {"FN1": ["21.76", "35.57"], "FN3": ["175.00", "159.74"],
                    "announce_time": ["20260430", "20260813"]}
        return {}

    monkeypatch.setattr(tqv, "tq_rpc", _rpc, raising=False)
    from src.core import tdx_corporate as tc

    tc._CACHE.clear()
    return tc


def test_block_trade_amount_wan_to_yuan(stub_gp):
    out = stub_gp.block_trades("600519")
    assert out["available"] is True
    item = out["items"][0]
    assert item["price"] == pytest.approx(1312.97)
    assert item["amount"] == pytest.approx(407.02 * 1e4), "GP04 成交额是万元，必须 ×1e4"
    assert out["amount_total"] == pytest.approx(407.02 * 1e4)


def test_holder_change_keeps_shares_raw(stub_gp):
    out = stub_gp.holder_changes("600519")
    item = out["items"][0]
    assert item["price"] == pytest.approx(1443.14)
    assert item["shares"] == pytest.approx(1274234), "GP05 变动股数是股，不能再乘 1e4"


def test_pledge_cross_validates_ratio_and_units(stub_gp):
    """GP19(万股) 与 GP20(%) 必须互相印证：6354 万 / 95103.5 万 = 6.68%。"""
    out = stub_gp.pledge("002361")
    latest = out["latest"]
    assert latest["shares_wan"] == pytest.approx(6354.0)
    assert latest["ratio_pct"] == pytest.approx(6.68)
    total_shares_wan = 95103.5
    assert latest["shares_wan"] / total_shares_wan * 100 == pytest.approx(6.68, abs=0.02)


def test_northbound_delta_uses_share_units(stub_gp):
    out = stub_gp.northbound_holding("600519")
    assert out["count"] == 2
    assert out["delta"] == pytest.approx(36320), "持股量差值单位=股"


def test_financials_pairs_dates_with_values(stub_gp):
    out = stub_gp.financials("600519")
    assert [i["date"] for i in out["items"]] == ["20260430", "20260813"]
    assert out["items"][-1]["eps"] == pytest.approx(35.57)
    assert out["items"][-1]["undistributed_ps"] == pytest.approx(159.74)


def test_empty_is_explicit_not_zero(monkeypatch):
    """TQ 返回空 → available=True + count=0 + 明确 note；绝不补 0。"""
    import marketdata.vendors.tq as tqv

    monkeypatch.setattr(tqv, "tq_rpc", lambda m, p, **k: {"GP04": []}, raising=False)
    from src.core import tdx_corporate as tc

    tc._CACHE.clear()
    out = tc.block_trades("002361")
    assert out["count"] == 0
    assert out["items"] == []
    assert out["amount_total"] is None
    assert "无大宗交易记录" in (out["note"] or "")


def test_dossier_survives_single_section_failure(monkeypatch):
    """单类异常不能拖垮其它类。"""
    from src.core import tdx_corporate as tc

    def boom(*a, **k):
        raise RuntimeError("TQ down")

    monkeypatch.setattr(tc, "block_trades", boom, raising=False)
    monkeypatch.setattr(tc, "holder_changes", lambda *a, **k: {"available": True, "items": []})
    out = tc.dossier("600519")
    assert out["block_trades"]["available"] is False
    assert out["holder_changes"]["available"] is True
