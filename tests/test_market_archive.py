"""批次2(2026-09-10): 市场数据落库(竞价快照 / 筹码日频)回归。

覆盖: 建表(迁移 SQLite 分支) → 幂等 upsert(同键覆盖不新增) → 读取 → 空值不伪造 → 失败安全。
表由 `src/web/migrations.py` 的 `_m154`/`_m155` 建(非 ORM), 这里直接用那两个函数建。
"""
from __future__ import annotations

import src.db.session as dbs
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from src.core import market_archive as ma
from src.web.migrations import (
    _m154_auction_snapshots_table,
    _m155_chip_daily_table,
)


def _mk_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m154_auction_snapshots_table(conn)
        _m155_chip_daily_table(conn)
    return eng


def _patch(monkeypatch):
    eng = _mk_engine()
    monkeypatch.setattr(dbs, "engine", eng)
    return eng


def _count(eng, table):
    with eng.connect() as conn:
        return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def test_auction_persist_read_and_idempotent_overwrite(monkeypatch):
    eng = _patch(monkeypatch)
    snaps = {
        "002361": {"source": "tq", "open": 10.10, "open_pct": -0.20, "open_amount": 2_103_800.0},
        "600769": {"source": "thsdk", "direction": "低开", "gap_pct": -0.305, "withdraw_rate_pre0920": 0.8388},
    }
    assert ma.persist_auction_snapshots("2026-09-10", snaps) == 2
    assert _count(eng, "auction_snapshots") == 2

    rows = ma.read_auction_snapshots("002361")
    assert len(rows) == 1
    assert rows[0]["open_price"] == 10.10
    assert rows[0]["open_pct"] == -0.20
    assert rows[0]["source"] == "tq"

    # 同键重跑 → 覆盖, 不新增
    ma.persist_auction_snapshots("2026-09-10", {"002361": {"open_pct": -0.55}})
    assert _count(eng, "auction_snapshots") == 2
    rows = ma.read_auction_snapshots("002361")
    assert rows[0]["open_pct"] == -0.55


def test_auction_empty_and_missing_fields(monkeypatch):
    eng = _patch(monkeypatch)
    assert ma.persist_auction_snapshots("2026-09-10", {}) == 0
    # 字段缺失 → NULL(不伪造 0)
    ma.persist_auction_snapshots("2026-09-10", {"000001": {"direction": "平开"}})
    row = ma.read_auction_snapshots("000001")[0]
    assert row["open_pct"] is None and row["open_amount"] is None
    assert row["direction"] == "平开"
    assert ma.read_auction_snapshots("999999") == []


def test_chip_persist_read_and_overwrite(monkeypatch):
    eng = _patch(monkeypatch)
    chips = {
        "cost_10": 9.5, "cost_50": 10.2, "cost_90": 11.8,
        "profit_ratio": 0.42, "peak_price": 10.3,
        "cost_band": {"low": 9.79, "high": 10.82, "ratio": 55.0},
    }
    assert ma.persist_chip_daily("2026-09-10", "002361", "CN", chips) is True
    assert _count(eng, "chip_daily") == 1
    r = ma.read_chip_daily("002361")[0]
    assert r["cost_50"] == 10.2 and r["peak_price"] == 10.3
    assert r["cost_band_low"] == 9.79 and r["cost_band_high"] == 10.82

    # 同日重算覆盖
    ma.persist_chip_daily("2026-09-10", "002361", "CN", {**chips, "cost_50": 10.9})
    assert _count(eng, "chip_daily") == 1
    assert ma.read_chip_daily("002361")[0]["cost_50"] == 10.9


def test_chip_empty_not_persisted(monkeypatch):
    _patch(monkeypatch)
    assert ma.persist_chip_daily("2026-09-10", "002361", "CN", None) is False
    assert ma.persist_chip_daily("2026-09-10", "002361", "CN", {}) is False
    # 空壳(无 cost_50/peak) 不落库
    assert ma.persist_chip_daily("2026-09-10", "002361", "CN", {"cost_10": 1.0}) is False


def test_read_never_raises_when_table_missing(monkeypatch):
    """未建表(或库不可用)时读取返回 [], 不抛 —— 落库失败不能拖垮主流程。"""
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(dbs, "engine", eng)
    assert ma.read_auction_snapshots("002361") == []
    assert ma.read_chip_daily("002361") == []
