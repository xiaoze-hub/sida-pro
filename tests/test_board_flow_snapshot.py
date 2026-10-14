"""板块资金采样落库(2026-10-14, 迁移 m191)。

审计定性: `/market-data/board-capital-flow` **完全现拉**同花顺行业/概念资金, 仅用
biz_cache 的 stale 备份兜源故障。本文件锁定收口后契约:

  ① 采样落库 + 读回: write_snapshot → read_fresh 原样返回(items 整块);
  ② 读侧新鲜度: 够新读库(零回源); 过旧/非交易时段/库空 → None(调用方回源);
  ③ 阈值可配: BOARD_FLOW_SNAPSHOT_FRESH_SEC 覆盖, 非法回落默认;
  ④ 端点『够新读库』: 库里有够新快照 → 直接读库, 零回源;
  ⑤ 端点『过旧回源』: 过旧行 → 回源实时并顺带采样落库(节流);
  ⑥ 端点源故障 + 有备份 → stale 标注(原语义不变); 空返回 + 无备份 → 显式 degraded;
  ⑦ 独立采样 collect_once: 交易时段落 industry+concept; 非交易时段跳过。

全部禁真实网络(vendor / TQ 均打桩, 库用内存 SQLite)。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import src.core.board_flow_sampler as bfs
import src.core.quote_snapshots as qs
import src.web.api.market_data as mdm
from src.web.migrations import _m191_board_flow_snapshots_table


def _mk_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m191_board_flow_snapshots_table(conn)
    return eng


def _use_engine(monkeypatch, eng):
    import src.db.session as dbs

    monkeypatch.setattr(dbs, "engine", eng)


def _items(net=(3.0, -1.0, 0.5)):
    return [
        {"board_name": f"板块{i}", "board_type": "industry", "index_value": 1.0,
         "change_pct": 1.0, "inflow": 10.0, "outflow": 5.0, "net_inflow": v,
         "stock_count": 10, "leader_name": "甲", "leader_change_pct": 2.0,
         "leader_price": 3.0, "rank": i + 1}
        for i, v in enumerate(net)
    ]


def _insert(eng, *, age_minutes: float = 0.0, board_type="industry", items=None):
    items = items if items is not None else _items()
    ts = "CURRENT_TIMESTAMP" if age_minutes == 0 else f"datetime('now', '-{age_minutes} minutes')"
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO board_flow_snapshots (ts, board_type, item_count, payload) "
                f"VALUES ({ts}, :bt, :n, :p)"
            ),
            {"bt": board_type, "n": len(items), "p": json.dumps(items, ensure_ascii=False)},
        )
    return eng


def _patch_trading(monkeypatch, flag=True):
    monkeypatch.setattr(qs, "in_trading_window", lambda now=None: flag)


class _Board:
    def __init__(self, name, net):
        self.board_name = name
        self.board_type = "industry"
        self.index_value = 1.0
        self.change_pct = 1.0
        self.inflow = 2.0
        self.outflow = 1.0
        self.net_inflow = net
        self.stock_count = 5
        self.leader_name = "龙头"
        self.leader_change_pct = 3.0
        self.leader_price = 4.0
        self.rank = 1


class _MD:
    def __init__(self, boards=None, raise_=False):
        self._boards = boards if boards is not None else []
        self._raise = raise_

    def board_capital_flow(self, *, board_type="industry"):
        if self._raise:
            raise RuntimeError("ths_flow down")
        return self._boards


def _fake_biz(monkeypatch) -> dict:
    store: dict = {}
    monkeypatch.setattr(mdm.biz_cache, "set_json", lambda k, v, ttl=None: store.__setitem__(k, v))
    monkeypatch.setattr(mdm.biz_cache, "get_json", lambda k: store.get(k))
    return store


# ─────────────────── ① 采样落库 + 读回 ───────────────────

def test_写快照后读回原样(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)
    items = _items()

    assert bfs.write_snapshot("industry", items) is True
    snap = bfs.read_fresh("industry")
    assert snap is not None
    assert snap["items"] == items
    assert snap["snapshot_age_sec"] <= 5 and snap["snapshot_ts"]


def test_空列表不落库(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    assert bfs.write_snapshot("industry", []) is False
    with eng.connect() as conn:
        assert int(conn.execute(text("SELECT COUNT(*) FROM board_flow_snapshots")).scalar()) == 0


# ─────────────────── ② 读侧新鲜度 ───────────────────

def test_过旧读库返回None(monkeypatch):
    eng = _insert(_mk_engine(), age_minutes=10)     # 10min > 默认 5min
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)
    assert bfs.read_fresh("industry") is None


def test_非交易时段返回None(monkeypatch):
    eng = _insert(_mk_engine(), age_minutes=0)
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, False)
    assert bfs.read_fresh("industry") is None


def test_库空返回None(monkeypatch):
    _use_engine(monkeypatch, _mk_engine())
    _patch_trading(monkeypatch, True)
    assert bfs.read_fresh("industry") is None


def test_库不可达静默返回None(monkeypatch):
    class _Boom:
        def connect(self):
            raise RuntimeError("db down")

    _use_engine(monkeypatch, _Boom())
    _patch_trading(monkeypatch, True)
    assert bfs.read_fresh("industry") is None


# ─────────────────── ③ 阈值可配 ───────────────────

def test_阈值环境变量覆盖(monkeypatch):
    monkeypatch.setenv("BOARD_FLOW_SNAPSHOT_FRESH_SEC", "900")
    eng = _insert(_mk_engine(), age_minutes=10)     # 600s < 900 → 够新
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)
    snap = bfs.read_fresh("industry")
    assert snap is not None and snap["snapshot_age_sec"] >= 590

    monkeypatch.setenv("BOARD_FLOW_SNAPSHOT_FRESH_SEC", "abc")   # 非法 → 默认
    assert bfs.read_fresh("industry") is None


# ─────────────────── ④ 端点够新读库零回源 ───────────────────

def test_端点够新读库零回源(monkeypatch):
    eng = _insert(_mk_engine(), age_minutes=0, items=_items())
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)
    _fake_biz(monkeypatch)

    def _no_md():
        raise AssertionError("库里有够新快照 → 不应回源实时")

    monkeypatch.setattr("src.core.marketdata_client.get_market_data", _no_md)
    out = asyncio.run(mdm.board_capital_flow_proxy(board_type="industry"))

    assert out["source"] == "db_snapshot"
    assert out["data_origin"] == "board_flow_snapshots"
    assert out["count"] == 3 and out["stale"] is False
    assert out["items"][0]["board_name"] == "板块0"
    assert out["snapshot_age_sec"] is not None


# ─────────────────── ⑤ 端点过旧回源 + 采样落库 ───────────────────

def test_端点过旧回源并采样落库(monkeypatch):
    eng = _insert(_mk_engine(), age_minutes=10)     # 过旧 → 回源
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)
    _fake_biz(monkeypatch)
    monkeypatch.setattr("src.core.marketdata_client.get_market_data",
                        lambda: _MD(boards=[_Board("银行", 5.0)]))

    written: list = []
    monkeypatch.setattr(bfs, "try_write_async", lambda bt, items: written.append((bt, items)))

    out = asyncio.run(mdm.board_capital_flow_proxy(board_type="industry"))
    assert out["source"] == "ths_realtime" and out["count"] == 1
    assert out["items"][0]["board_name"] == "银行" and out["snapshot_used"] is False
    assert written and written[0][0] == "industry" and written[0][1][0]["board_name"] == "银行"


def test_端点空返回无备份显式degraded(monkeypatch):
    _use_engine(monkeypatch, _mk_engine())
    _patch_trading(monkeypatch, True)
    _fake_biz(monkeypatch)
    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD(boards=[]))
    monkeypatch.setattr(mdm, "_stale_take", lambda kind: None)

    out = asyncio.run(mdm.board_capital_flow_proxy(board_type="industry"))
    assert out["degraded"] is True and out["count"] == 0 and "非" in out["note"]


def test_端点源故障有备份带stale标注(monkeypatch):
    import time as _t

    _use_engine(monkeypatch, _mk_engine())
    _patch_trading(monkeypatch, True)
    store = {"stale:board-flow:industry": {
        "saved_at": _t.time() - 120,
        "payload": {"board_type": "industry", "count": 1, "items": [{"board_name": "旧"}]},
    }}
    monkeypatch.setattr(mdm.biz_cache, "get_json", lambda k: store.get(k))
    monkeypatch.setattr(mdm.biz_cache, "set_json", lambda k, v, ttl=None: None)
    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _MD(raise_=True))

    out = asyncio.run(mdm.board_capital_flow_proxy(board_type="industry"))
    assert out["stale"] is True and out["items"][0]["board_name"] == "旧"
    assert 110 <= out["stale_age_sec"] <= 140


# ─────────────────── ⑦ 独立采样 collect_once ───────────────────

def test_collect_once交易时段落两类(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, True)
    monkeypatch.setattr("src.core.marketdata_client.get_market_data",
                        lambda: _MD(boards=[_Board("银行", 5.0)]))

    out = bfs.collect_once()
    assert out["ok"] is True and out["skipped"] is False
    assert out["written"] == {"industry": 1, "concept": 1}
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT board_type, item_count FROM board_flow_snapshots ORDER BY board_type"
        )).fetchall()
    assert [(r[0], r[1]) for r in rows] == [("concept", 1), ("industry", 1)]


def test_collect_once非交易时段跳过(monkeypatch):
    eng = _mk_engine()
    _use_engine(monkeypatch, eng)
    _patch_trading(monkeypatch, False)
    out = bfs.collect_once()
    assert out["skipped"] is True
    with eng.connect() as conn:
        assert int(conn.execute(text("SELECT COUNT(*) FROM board_flow_snapshots")).scalar()) == 0


def test_collect_once取数失败不抛(monkeypatch):
    _use_engine(monkeypatch, _mk_engine())
    _patch_trading(monkeypatch, True)

    class _Boom:
        def board_capital_flow(self, **k):
            raise RuntimeError("down")

    monkeypatch.setattr("src.core.marketdata_client.get_market_data", lambda: _Boom())
    out = bfs.collect_once()
    assert out["ok"] is True and out["written"] == {"industry": 0, "concept": 0}
