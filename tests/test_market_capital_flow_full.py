"""大盘资金端点全接入(2026-10-14 资金类落库收口)。

收口前 `market-capital-flow` 是**半接入**: 够新读 `market_flow_snapshots` 的资金/家数字段,
但板块明细(流入/流出榜)在库读分支**恒空**(快照表没存), 功能弱于实时分支。收口后:

  · 够新 → 读库, **板块明细从 board_flow_snapshots(m191) 读够新快照派生**, 与实时同口径;
  · 板块快照缺失/过旧 → 板块明细显式置空(不编造, board_source=None);
  · 无够新大盘资金快照 → 回源实时, 并**显式标注** snapshot_used=False + snapshot_note;
  · 过旧回源 / 库空回源 / 源故障 stale 备份 等原语义不变(见 test_market_flow_db_read)。

全部禁真实网络(网关 / vendor / 库均打桩)。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import src.core.quote_snapshots as qs
import src.web.api.market_data as mdm
from src.web.migrations import (
    _m138_market_flow_snapshots_table,
    _m193_board_flow_snapshots_table,
)


def _mk_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m138_market_flow_snapshots_table(conn)
        _m193_board_flow_snapshots_table(conn)
    return eng


def _use_engine(monkeypatch, eng):
    """市场资金读经 src.web.database.engine; 板块快照读经 board_flow_sampler
    (src.db.session.engine, core 层禁止 import src.web) —— 两处都要指到同一内存库。"""
    import src.db.session as dbs
    import src.web.database as database

    monkeypatch.setattr(database, "engine", eng)
    monkeypatch.setattr(dbs, "engine", eng)


def _patch_trading(monkeypatch, flag=True):
    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: flag)


def _insert_flow(eng, *, age_minutes: float = 0.0):
    ts = "CURRENT_TIMESTAMP" if age_minutes == 0 else f"datetime('now', '-{age_minutes} minutes')"
    with eng.begin() as conn:
        conn.execute(text(
            f"INSERT INTO market_flow_snapshots "
            f"(ts, total_main_flow, up_count, down_count, flat_count, sh_flow, sz_flow) "
            f"VALUES ({ts}, -123.4, 100, 200, 5, -60.0, -63.4)"
        ))
    return eng


def _insert_boards(eng, *, age_minutes: float = 0.0, items=None):
    items = items if items is not None else [
        {"board_name": "板块0", "net_inflow": 3.0, "change_pct": 1.0},
        {"board_name": "板块1", "net_inflow": -1.0, "change_pct": -1.0},
        {"board_name": "板块2", "net_inflow": 0.5, "change_pct": 0.2},
    ]
    ts = "CURRENT_TIMESTAMP" if age_minutes == 0 else f"datetime('now', '-{age_minutes} minutes')"
    with eng.begin() as conn:
        conn.execute(
            text("INSERT INTO board_flow_snapshots (ts, board_type, item_count, payload) "
                 f"VALUES ({ts}, 'industry', :n, :p)"),
            {"n": len(items), "p": json.dumps(items, ensure_ascii=False)},
        )
    return eng


@pytest.fixture
def _no_realtime(monkeypatch):
    """实时链路打桩: 任何外呼都视为「不该发生」。"""
    def _boom(*a, **k):
        raise AssertionError("库读分支不应实时外呼")

    monkeypatch.setattr("requests.get", _boom)
    monkeypatch.setattr("src.core.marketdata_client.get_market_data", _boom)
    monkeypatch.setattr(mdm, "_try_write_snapshot_async", lambda payload: None)


# ─────────── ① 够新大盘资金 + 够新板块 → 全库读, 板块明细同口径 ───────────

def test_库读分支含板块明细(monkeypatch, _no_realtime):
    eng = _insert_boards(_insert_flow(_mk_engine()))
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)

    out = asyncio.run(mdm.market_capital_flow_proxy())

    assert out["source"] == "db_snapshot" and out["snapshot_used"] is True
    assert out["total_main_flow"] == -123.4
    # 板块明细来自板块资金快照(不再是恒空)
    assert out["board_source"] == "db_snapshot"
    assert out["inflow_boards"] == [
        {"name": "板块0", "net_inflow": 3.0, "change_pct": 1.0},
        {"name": "板块2", "net_inflow": 0.5, "change_pct": 0.2},
    ]
    assert out["outflow_boards"] == [
        {"name": "板块1", "net_inflow": -1.0, "change_pct": -1.0},
    ]


def test_板块快照缺失_显式置空不编造(monkeypatch, _no_realtime):
    eng = _insert_flow(_mk_engine())        # 只落资金, 不落板块
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)

    out = asyncio.run(mdm.market_capital_flow_proxy())
    assert out["source"] == "db_snapshot"
    assert out["inflow_boards"] == [] and out["outflow_boards"] == []
    assert out["board_source"] is None      # 无够新板块快照 → 显式无来源, 不编造


def test_板块快照过旧_置空(monkeypatch, _no_realtime):
    eng = _insert_boards(_insert_flow(_mk_engine()), age_minutes=10)
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)

    out = asyncio.run(mdm.market_capital_flow_proxy())
    assert out["inflow_boards"] == [] and out["board_source"] is None


# ─────────── ② 无够新大盘资金快照 → 回源 + 显式标注 ───────────

def test_无快照回源并显式标注(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)
    data = {
        "total_main_flow": -100.0, "sh": {"main_flow": -50.0}, "sz": {"main_flow": -50.0},
        "cyb": {"main_flow": -10.0}, "total_amount": 16000.0,
        "up_count": 900, "down_count": 4200, "flat_count": 100,
    }

    class _Resp:
        def json(self):
            return data

    class _Board:
        def __init__(self):
            self.board_name = "银行"
            self.board_type = "industry"
            self.index_value = 1.0
            self.change_pct = 1.0
            self.inflow = 2.0
            self.outflow = 1.0
            self.net_inflow = 5.0
            self.stock_count = 5
            self.leader_name = "甲"
            self.leader_change_pct = 3.0
            self.leader_price = 4.0
            self.rank = 1

    class _MD:
        def board_capital_flow(self, *, board_type="industry"):
            return [_Board()]

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD())
    monkeypatch.setattr(mdm, "_try_write_snapshot_async", lambda payload: None)
    monkeypatch.setattr(mdm, "_tq_breadth", lambda: None)

    import src.core.board_flow_sampler as bfs
    monkeypatch.setattr(bfs, "try_write_async", lambda bt, items: None)

    out = asyncio.run(mdm.market_capital_flow_proxy())
    assert out["source"] == "eastmoney_push2delay_cn"
    assert out["snapshot_used"] is False
    assert "无够新" in out["snapshot_note"]
    # 实时分支的板块明细走同一 _boards_to_inout 口径
    assert out["inflow_boards"] == [{"name": "银行", "net_inflow": 5.0, "change_pct": 1.0}]
    assert out["outflow_boards"] == []


# ─────────── ③ 板块排序口径 ───────────

def test_boards_to_inout口径(monkeypatch):
    items = [
        {"board_name": "a", "net_inflow": 1.0, "change_pct": 0.1},
        {"board_name": "b", "net_inflow": None, "change_pct": 0.2},   # 缺失不参与排序
        {"board_name": "c", "net_inflow": 5.0, "change_pct": 0.3},
        {"board_name": "d", "net_inflow": -3.0, "change_pct": -0.4},
        {"board_name": "e", "net_inflow": -1.0, "change_pct": -0.5},
    ]
    inflow, outflow = mdm._boards_to_inout(items)
    assert [x["name"] for x in inflow] == ["c", "a"]          # 降序, 只正
    assert [x["name"] for x in outflow] == ["d", "e"]         # 最负在前, 只负
    assert mdm._boards_to_inout([]) == ([], [])
