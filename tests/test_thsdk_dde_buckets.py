"""thsdk 双L2修补单测(2026-09-05, v0.4.98)。

覆盖:
- bucket_dde_orders: 金额四档 + 已知方向(1买/5卖) + 未知方向单列 + 空表
- get_hs300_constituents: TQ 回退成功 / TQ 异常回空表
- get_comprehensive_snapshot: 30s 缓存命中(第二次不触发新查询)
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pandas as pd
import pytest


def _make_fake_thsdk():
    fake = types.ModuleType("thsdk")

    class _THS:
        def __init__(self, cfg=None):
            self.cfg = cfg or {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake.THS = _THS
    return fake


_real_thsdk = sys.modules.get("thsdk")
sys.modules["thsdk"] = _make_fake_thsdk()
try:
    import data_source.thsdk_l2 as M
finally:
    if _real_thsdk is not None:
        sys.modules["thsdk"] = _real_thsdk
    else:
        sys.modules.pop("thsdk", None)


def _orders_df():
    # 金额_万元: 150买(特大)/80卖(大)/10买(中)/2卖(小)/50方向15(未知)/30方向0(中性)
    return pd.DataFrame({
        "成交方向": [1, 5, 1, 5, 15, 0],
        "金额_万元": [150.0, 80.0, 10.0, 2.0, 50.0, 30.0],
    })


class TestBucketDdeOrders:
    def test_four_buckets(self):
        out = M.THSDKL2.bucket_dde_orders(_orders_df())
        assert list(out["分档"]) == ["特大单", "大单", "中单", "小单", "未知方向"]
        super_row = out.iloc[0]
        assert super_row["买入万元"] == 150.0 and super_row["净额万元"] == 150.0
        big_row = out.iloc[1]
        assert big_row["卖出万元"] == 80.0 and big_row["净额万元"] == -80.0
        mid_row = out.iloc[2]
        # 方向0的中性30万不计入买卖
        assert mid_row["买入万元"] == 10.0 and mid_row["卖出万元"] == 0.0
        small_row = out.iloc[3]
        assert small_row["卖出万元"] == 2.0
        unknown_row = out.iloc[4]
        # 方向15的50万单列, 不计入任何档买卖
        assert unknown_row["未知方向大额万元"] == 50.0
        assert out["买入万元"].sum() == 160.0

    def test_empty(self):
        out = M.THSDKL2.bucket_dde_orders(pd.DataFrame())
        assert len(out) == 0
        out2 = M.THSDKL2.bucket_dde_orders(None)  # type: ignore[arg-type]
        assert len(out2) == 0


class TestHs300Fallback:
    def test_tq_success(self, monkeypatch):
        fake_tq = types.ModuleType("marketdata.vendors.tq")
        fake_tq._rpc = MagicMock(return_value=[
            {"Code": "600519", "Name": "贵州茅台"},
            {"Code": "000001", "Name": "平安银行"},
        ])
        fake_pkg = types.ModuleType("marketdata.vendors")
        fake_top = types.ModuleType("marketdata")
        monkeypatch.setitem(sys.modules, "marketdata.vendors.tq", fake_tq)
        monkeypatch.setitem(sys.modules, "marketdata.vendors", fake_pkg)
        monkeypatch.setitem(sys.modules, "marketdata", fake_top)
        t = M.THSDKL2.__new__(M.THSDKL2)
        out = t.get_hs300_constituents()
        assert list(out["代码"]) == ["600519", "000001"]
        assert list(out["名称"]) == ["贵州茅台", "平安银行"]

    def test_tq_failure_empty(self, monkeypatch):
        fake_tq = types.ModuleType("marketdata.vendors.tq")

        def _boom(*a, **k):
            raise RuntimeError("TQ down")

        fake_tq._rpc = _boom
        fake_pkg = types.ModuleType("marketdata.vendors")
        fake_top = types.ModuleType("marketdata")
        monkeypatch.setitem(sys.modules, "marketdata.vendors.tq", fake_tq)
        monkeypatch.setitem(sys.modules, "marketdata.vendors", fake_pkg)
        monkeypatch.setitem(sys.modules, "marketdata", fake_top)
        t = M.THSDKL2.__new__(M.THSDKL2)
        out = t.get_hs300_constituents()
        assert len(out) == 0 and list(out.columns) == ["代码", "名称"]


class TestSnapshotCache:
    def test_cache_hit(self, monkeypatch):
        M._SNAPSHOT_CACHE.clear()
        t = M.THSDKL2.__new__(M.THSDKL2)
        calls = {"n": 0}

        def _fake_quote(symbol):
            calls["n"] += 1
            return {"now": 1}

        monkeypatch.setattr(t, "get_quote", _fake_quote)
        monkeypatch.setattr(t, "get_depth", lambda s: {})
        monkeypatch.setattr(t, "get_order_book_20", lambda s: ([1], [1]))
        monkeypatch.setattr(t, "get_intraday", lambda s: [1])
        monkeypatch.setattr(t, "compute_main_flow", lambda s: {})
        r1 = t.get_comprehensive_snapshot("USZA002361")
        r2 = t.get_comprehensive_snapshot("USZA002361")
        assert r1["cached"] is False and r2["cached"] is True
        assert calls["n"] == 1
        M._SNAPSHOT_CACHE.clear()


class TestGetDdeOfficial:
    def test_split_symbol(self):
        assert M.THSDKL2._split_symbol("USZA002361") == ("002361", "USZA")
        assert M.THSDKL2._split_symbol("600519") == ("600519", "USHA")
        assert M.THSDKL2._split_symbol("300010") == ("300010", "USZA")
        assert M.THSDKL2._split_symbol("430047") == ("430047", "USTM")

    def test_official_passthrough(self, monkeypatch):
        t = M.THSDKL2.__new__(M.THSDKL2)
        official = pd.DataFrame([{"主动买入特大单金额": 100.0, "代码": "002361"}])
        monkeypatch.setattr(t, "get_dde_flow", lambda six, market="USHA", detail=True: official)
        out = t.get_dde("USZA002361")
        assert "主动买入特大单金额" in out.columns

    def test_fallback_buckets(self, monkeypatch):
        t = M.THSDKL2.__new__(M.THSDKL2)

        def _boom(*a, **k):
            raise RuntimeError("query_data down")

        monkeypatch.setattr(t, "get_dde_flow", _boom)
        monkeypatch.setattr(t, "get_big_orders", lambda symbol: _orders_df())
        out = t.get_dde("USZA002361")
        assert list(out["分档"]) == ["特大单", "大单", "中单", "小单", "未知方向"]
