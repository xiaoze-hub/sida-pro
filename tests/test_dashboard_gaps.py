"""首页两处 `--` 修复（2026-10-20）。

1. 跌停家数：`/market/phase` 原本没有这个字段，前端写死 `--`（KpiBand 里的 `<span>--</span>`）。
   现改为从全市场涨跌幅分布快照的「跌停」档派生，读不到才 `--`。
2. 两市成交额：资金快照表没存 total_amount，走库快照分支时被显式置空 → 首页成交额恒为 `--`。
   补列后快照分支也能返回成交额。
"""
from __future__ import annotations

import json
from datetime import datetime

from src.db.models import BreadthDistributionSnapshot
from src.db.session import SessionLocal
from src.web.api import market_data as md
from src.web.api import market_phase as mp


def _seed_breadth(items: list[dict]) -> None:
    db = SessionLocal()
    try:
        db.query(BreadthDistributionSnapshot).delete()
        db.add(BreadthDistributionSnapshot(
            trade_date="2026-10-20", captured_at=datetime.now(),
            total=5600, valid=5600,
            items=json.dumps(items, ensure_ascii=False), source="test",
        ))
        db.commit()
    finally:
        db.close()


def test_limit_down_comes_from_breadth_snapshot():
    _seed_breadth([{"bucket": "跌停", "count": 7}, {"bucket": "涨停", "count": 50}])
    assert mp._limit_down_from_breadth() == 7


def test_limit_down_is_none_when_no_snapshot():
    db = SessionLocal()
    try:
        db.query(BreadthDistributionSnapshot).delete()
        db.commit()
    finally:
        db.close()
    assert mp._limit_down_from_breadth() is None


def test_snapshot_response_now_carries_total_amount():
    resp = md._snapshot_to_response({
        "total_main_flow": -56.9, "sh_flow": 28.4, "sz_flow": -85.3,
        "up_count": 3021, "down_count": 2368, "flat_count": 100,
        "total_amount": 12345.6,
    })
    assert resp["total_amount"] == 12345.6
    assert resp["total_main_flow"] == -56.9
    # 未存的字段仍然显式置空，不编造
    assert resp["cyb_flow"] is None


def test_snapshot_response_keeps_null_when_amount_missing():
    resp = md._snapshot_to_response({"total_main_flow": 1.0})
    assert resp["total_amount"] is None


def test_total_amount_falls_back_to_index_turnover(monkeypatch):
    """上游 total_amount 为 None 时用指数成交额兜底（首页成交额不再恒为 --）。"""
    class _MD:
        def index_quotes(self, codes):
            assert set(codes) == {"sh000001", "sz399001"}
            return [{"turnover": 3.9e11}, {"turnover": 4.37e11}]

    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD())
    md._AMOUNT_CACHE.update({"ts": 0.0, "val": None})
    assert md._total_amount_from_indices() == 8270.0


def test_total_amount_none_when_source_incomplete(monkeypatch):
    class _MD:
        def index_quotes(self, codes):
            return [{"turnover": 3.9e11}]

    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD())
    md._AMOUNT_CACHE.update({"ts": 0.0, "val": None})
    assert md._total_amount_from_indices() is None
