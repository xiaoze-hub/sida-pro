# -*- coding: utf-8 -*-
""".tck 暗盘精确复算 hook(A4) 单测: chat_tools.get_dark_flow_precise

覆盖:
  - 无 .tck 文件 → error 非空, data=None
  - 有 .tck → 主笔级(active/passive) + dark_clusters(拆单簇暗盘) 并存
  - dark_review_from_tck 抛异常 → dark_clusters available=False, 主笔级不受影响
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.core import chat_tools as ct  # noqa: E402


def _mock_tck_deps(monkeypatch, review_result=None, review_error=None):
    """mock find_tck_file / parse_tck / dark_flow_from_tck / dark_review_from_tck。"""
    import src.core.dark_split as ds
    import src.core.tdx_tick_parser as tp
    import src.core.postmarket_review as pr

    monkeypatch.setattr(ds, "find_tck_file", lambda code, date_=None: "fake.tck")
    monkeypatch.setattr(
        tp, "parse_tck",
        lambda path: (
            [{"amt": 400000.0, "dir": "B"}, {"amt": 200000.0, "dir": "S"}],
            [{"amt": 100000.0, "a28": None, "a32": None}],
            [],
        ),
    )
    monkeypatch.setattr(
        ds, "dark_flow_from_tck",
        lambda trades, orders, threshold_yuan=300000: {
            "active_buy": 400000.0, "active_sell": 200000.0, "active_net": 200000.0,
            "passive_buy": 0.0, "passive_sell": 0.0, "passive_net": 0.0,
            "net": 200000.0, "ming_net": 400000.0, "small_net": -200000.0,
            "trade_count": 2, "order_count": 1, "partial": True,
            "dark_basis": "small_orders", "note": "仅主笔级还原",
        },
    )
    if review_error:
        monkeypatch.setattr(
            pr, "dark_review_from_tck",
            lambda symbol, date_=None, tck_path=None: (_ for _ in ()).throw(review_error),
        )
    else:
        monkeypatch.setattr(
            pr, "dark_review_from_tck",
            lambda symbol, date_=None, tck_path=None: review_result,
        )


def test_no_tck_file(monkeypatch):
    import src.core.dark_split as ds

    monkeypatch.setattr(ds, "find_tck_file", lambda code, date_=None: None)
    r = ct.get_dark_flow_precise("002361")
    assert r.error is not None
    assert r.data is None
    assert "无 .tck" in r.error


def test_with_clusters_hooked(monkeypatch):
    review = {
        "symbol": "002361", "date": "2026-09-01", "available": True,
        "ming": {"net": 400000.0, "buy": 400000.0, "sell": 0.0, "count": 1},
        "dark": {"net": -200000.0, "buy": 0.0, "sell": 200000.0, "count": 2},
        "main_net": 200000.0,
        "cancel_rate": 0.05, "active_passive_ratio": 1.2,
        "clusters": [{"side": "卖", "count": 2, "amount": 200000.0}],
        "note": None,
    }
    _mock_tck_deps(monkeypatch, review_result=review)

    r = ct.get_dark_flow_precise("002361")
    assert r.error is None
    d = r.data
    # 主笔级(现有)保留
    assert d["active_net"] == 200000.0
    assert d["dark_basis"] == "small_orders"
    # A4 新增 dark_clusters(拆单簇暗盘)
    assert d["dark_clusters"]["available"] is True
    assert d["dark_clusters"]["dark_net"] == -200000.0
    assert d["dark_clusters"]["cluster_count"] == 1
    assert d["dark_clusters"]["main_net"] == 200000.0
    assert d["dark_clusters"]["cancel_rate"] == 0.05
    assert d["tck_path"] == "fake.tck"


def test_review_error_degrades(monkeypatch):
    _mock_tck_deps(monkeypatch, review_error=RuntimeError("boom"))
    r = ct.get_dark_flow_precise("002361")
    assert r.error is None  # 主笔级仍正常
    d = r.data
    assert d["active_net"] == 200000.0
    assert d["dark_clusters"]["available"] is False
    assert "boom" in d["dark_clusters"]["note"]
