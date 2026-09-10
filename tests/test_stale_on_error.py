"""C2 stale-on-error (2026-09-10): 展示类资金流端点在源故障时回退"上次成功快照 + 显式标注"。
约束: 仅展示类(板块/大盘资金); 行情/结算路径严禁复用 —— 本文件同时锁定"无备份仍 502"底线。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import HTTPException

import src.core.marketdata_client as md_client
import src.web.api.market_data as mdm


class _Board:
    def __init__(self, name: str):
        self.board_name = name
        self.board_type = "industry"
        self.index_value = 1.0
        self.change_pct = 1.0
        self.inflow = 1.0
        self.outflow = 0.5
        self.net_inflow = 0.5
        self.stock_count = 10
        self.leader_name = "甲"
        self.leader_change_pct = 2.0
        self.leader_price = 3.0
        self.rank = 1


class _MD:
    def __init__(self, *, boards=None, raise_board=False):
        self._boards = boards if boards is not None else []
        self._raise = raise_board

    def board_capital_flow(self, *, board_type="industry"):
        if self._raise:
            raise RuntimeError("ths_flow down")
        return self._boards


def _fake_biz(monkeypatch) -> dict:
    """把 biz_cache 换成进程内字典(避免测试触 Redis)。"""
    store: dict = {}
    monkeypatch.setattr(mdm.biz_cache, "set_json", lambda k, v, ttl=None: store.__setitem__(k, v))
    monkeypatch.setattr(mdm.biz_cache, "get_json", lambda k: store.get(k))
    return store


# ────────────────────────── 板块资金 ──────────────────────────

def test_board_flow_success_writes_stale_backup(monkeypatch):
    store = _fake_biz(monkeypatch)
    monkeypatch.setattr(md_client, "get_market_data", lambda: _MD(boards=[_Board("银行")]))

    out = asyncio.run(mdm.board_capital_flow_proxy(board_type="industry"))

    assert out["count"] == 1
    assert "stale" not in out  # 新鲜数据不标
    rec = store.get("stale:board-flow:industry")
    assert rec and rec["payload"]["items"][0]["board_name"] == "银行"


def test_board_flow_failure_serves_stale_with_marker(monkeypatch):
    store = _fake_biz(monkeypatch)
    store["stale:board-flow:industry"] = {
        "saved_at": time.time() - 300,
        "payload": {"board_type": "industry", "count": 1, "items": [{"board_name": "旧银行"}]},
    }
    monkeypatch.setattr(md_client, "get_market_data", lambda: _MD(raise_board=True))

    out = asyncio.run(mdm.board_capital_flow_proxy(board_type="industry"))

    assert out["stale"] is True
    assert out["items"][0]["board_name"] == "旧银行"
    assert 290 <= out["stale_age_sec"] <= 320


def test_board_flow_failure_without_backup_still_502(monkeypatch):
    _fake_biz(monkeypatch)
    monkeypatch.setattr(md_client, "get_market_data", lambda: _MD(raise_board=True))

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mdm.board_capital_flow_proxy(board_type="industry"))
    assert ei.value.status_code == 502


def test_board_flow_empty_with_backup_serves_stale(monkeypatch):
    """源返回空列表(异常语义)且有备份 → 旧数据+标注, 不给 count:0 假空。"""
    store = _fake_biz(monkeypatch)
    store["stale:board-flow:industry"] = {
        "saved_at": time.time() - 60,
        "payload": {"board_type": "industry", "count": 1, "items": [{"board_name": "旧"}]},
    }
    monkeypatch.setattr(md_client, "get_market_data", lambda: _MD(boards=[]))

    out = asyncio.run(mdm.board_capital_flow_proxy(board_type="industry"))
    assert out["stale"] is True and out["count"] == 1


# ────────────────────────── 大盘资金 ──────────────────────────

def _gateway_ok():
    return {"total_main_flow": -100.0, "sh": {"main_flow": -50.0, "point": 390000, "change_pct": -30},
            "sz": {"main_flow": -50.0}, "cyb": {"main_flow": -10.0},
            "total_amount": 16000.0, "up_count": 900, "down_count": 4200, "flat_count": 100}


def test_market_flow_success_writes_stale_backup(monkeypatch):
    store = _fake_biz(monkeypatch)
    monkeypatch.setattr(md_client, "get_market_data", lambda: _MD(boards=[_Board("银行")]))
    monkeypatch.setattr(mdm, "_try_write_snapshot_async", lambda payload: None)
    monkeypatch.setattr("requests.get", lambda *a, **k: type("R", (), {"json": staticmethod(_gateway_ok)})())

    out = asyncio.run(mdm.market_capital_flow_proxy())

    assert out["total_main_flow"] == -100.0
    assert "stale" not in out
    assert store.get("stale:market-flow") is not None


def test_market_flow_failure_serves_stale_with_marker(monkeypatch):
    store = _fake_biz(monkeypatch)
    store["stale:market-flow"] = {
        "saved_at": time.time() - 180,
        "payload": {"total_main_flow": -88.0, "inflow_boards": [], "outflow_boards": [], "stale": False},
    }

    def _boom(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr("requests.get", _boom)
    monkeypatch.setattr(md_client, "get_market_data", lambda: _MD(boards=[]))

    out = asyncio.run(mdm.market_capital_flow_proxy())

    assert out["stale"] is True
    assert out["total_main_flow"] == -88.0
    assert 170 <= out["stale_age_sec"] <= 200


def test_market_flow_failure_without_backup_still_502(monkeypatch):
    _fake_biz(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr("requests.get", _boom)
    monkeypatch.setattr(md_client, "get_market_data", lambda: _MD(boards=[]))

    with pytest.raises(HTTPException) as ei:
        asyncio.run(mdm.market_capital_flow_proxy())
    assert ei.value.status_code == 502
