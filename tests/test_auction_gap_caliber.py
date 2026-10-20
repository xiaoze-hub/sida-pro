"""竞价涨幅口径：必须是「开盘价 vs 昨收」，不能用 THS 异动幅度冒充（2026-10-20 事故）。

事故经过：金健米业(600127) 当日 **低开**（开 12.93 / 昨收 13.16 = **-1.75%**），
但集合竞价页显示「竞价涨幅 **+4.87%**」并判定「爆量抢筹 · 超预期高开」。

根因：THS 集合竞价异动表的「价格」列被直接 ×100 当成相对昨收的涨幅，而这一列是
**9:25 那一瞬的异动幅度**（类型为「急速上涨 / 急速下跌」时尤其如此），与开盘方向可以
完全相反。旧代码里它优先于 TQ 的 `OpenZAF`（真实开盘竞价涨跌幅）落值，于是低开被讲成高开。
"""
from __future__ import annotations

import sys
import types

import pandas as pd
import pytest

import src.core.auction_pool as ap


def _anomaly_df() -> pd.DataFrame:
    return pd.DataFrame([{
        "时间": 1792459502,
        "价格": 0.0487,
        "总金额": 1,
        "代码": "600127",
        "名称": "金健米业",
        "异动类型1": "急速上涨",
    }])


@pytest.fixture()
def fake_sources(monkeypatch):
    import marketdata.vendors.tq as tq_mod

    # thsdk 本地未安装 → 用桩模块替换整个 data_source.thsdk_l2
    stub = types.ModuleType("data_source.thsdk_l2")
    stub.get_auction_anomaly = lambda market="USHA": _anomaly_df()
    monkeypatch.setitem(sys.modules, "data_source.thsdk_l2", stub)
    infos = {
        "600127": {"Open": 12.93, "LastClose": 13.16, "OpenZAF": None,
                   "OpenAmo": 17941700.0, "OpenVolPre1": 3.22},
    }
    monkeypatch.setattr(
        tq_mod, "_rpc",
        lambda method, params=None: infos.get((params or {}).get("stock_code", "").split(".")[0], {})
        if method == "get_more_info" else {},
        raising=False,
    )
    ap.clear_cache()


def test_gap_uses_open_vs_prev_close_not_anomaly_ratio(fake_sources):
    """600127：THS 异动 +4.87%，真实低开 -1.75% → gap_pct 必须是 -1.75。"""
    recs = ap.fetch_auction_anomaly("CN")
    row = next((r for r in recs if r.get("symbol") == "600127"), None)
    assert row is not None, "未产出 600127 记录"
    assert row["gap_pct"] == -1.75, f"竞价涨幅口径错: {row['gap_pct']}"
    assert row["gap_source"] == "tq_open_vs_lastclose"
    assert row["anomaly_ratio"] == 4.87, "THS 异动幅度应保留在 anomaly_ratio"
    assert "(盘中异动)" in row["anomaly_type"]
    assert row["verdict_tag"] not in ("🔥 爆量抢筹", "👑 涨停抢筹")
    assert "高开" not in (row.get("verdict_action") or "")


def test_gap_none_when_open_or_prevclose_missing(monkeypatch):
    """拿不到开盘价/昨收 → gap_pct 置空 + 数据不足，绝不退回异动幅度。"""
    import marketdata.vendors.tq as tq_mod

    # thsdk 本地未安装 → 用桩模块替换整个 data_source.thsdk_l2
    stub = types.ModuleType("data_source.thsdk_l2")
    stub.get_auction_anomaly = lambda market="USHA": _anomaly_df()
    monkeypatch.setitem(sys.modules, "data_source.thsdk_l2", stub)
    monkeypatch.setattr(tq_mod, "_rpc", lambda m, p=None: {}, raising=False)
    ap.clear_cache()

    recs = ap.fetch_auction_anomaly("CN")
    row = next((r for r in recs if r.get("symbol") == "600127"), None)
    assert row is not None
    assert row["gap_pct"] is None
    assert row["gap_source"] == "unavailable"
    assert row["anomaly_ratio"] == 4.87
    assert row["verdict_tag"] == "❔ 数据不足"
