# -*- coding: utf-8 -*-
"""主力资金战报(规格 §4.4, 2026-10-10) 单测。

覆盖:
  - build_daily_war_report 字段契约(TOP/BOTTOM + 行业分布 + 净额变化 + 拆单/对倒计数)
  - 缺源**显式降级**(行业/拆单对倒 available=False + note; 数据源全空 → 战报 available=False)
  - GET /api/war-report/daily 端点契约(无快照 → available:false + note; 有快照 → 透传 payload)
禁真网络: DDE 用 FakeL2, 行业/拆单用 monkeypatch, DB 用内存 sqlite。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


class FakeL2:
    def __init__(self, codes=None, nets=None):
        self._codes = codes or []
        self._nets = nets or {}

    def get_stock_cn_lists(self):
        return pd.DataFrame(
            {"代码": [c for c, _ in self._codes], "名称": [n for _, n in self._codes]}
        )

    def get_dde_flow(self, codelist, market="USZA", detail=False):
        rows = []
        for c in codelist.split(","):
            rows.append({
                "代码": f"{market}{c}",
                "主力净流入": float(self._nets.get(c, 0.0)),
                "主力净量": 0.01,
                "总金额": 1e7,
            })
        return pd.DataFrame(rows)


@pytest.fixture()
def wr_db(monkeypatch):
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    from src.web.migrations import _m185_dde_minute_flow_table, _m186_war_report_daily_table

    with eng.begin() as conn:
        _m185_dde_minute_flow_table(conn)
        _m186_war_report_daily_table(conn)

    import src.db.session as dbs

    monkeypatch.setattr(dbs, "engine", eng)
    monkeypatch.setattr(dbs, "SessionLocal", sessionmaker(bind=eng))
    return sessionmaker(bind=eng)()


def _patch_subblocks(monkeypatch, industry=None, split=None):
    from src.core import war_report as wr

    monkeypatch.setattr(
        wr, "industry_distribution",
        lambda top_n=20: industry if industry is not None
        else {"available": False, "note": "行业源不可用", "rows": []},
    )
    monkeypatch.setattr(
        wr, "split_wash_counts",
        lambda syms: split if split is not None
        else {"available": False, "split_count": None, "wash_count": None,
              "covered": [], "note": "无 .tck 源"},
    )


# ────────────────────────── builder 字段契约 ──────────────────────────
def test_war_report_build_fields_contract(wr_db, monkeypatch):
    from src.core import war_report as wr

    _patch_subblocks(monkeypatch)
    l2 = FakeL2(
        [("USZA000001", "A"), ("USZA000002", "B"), ("USHA600000", "C"), ("USHA600001", "D")],
        {"000001": 500e4, "000002": 200e4, "600000": -300e4, "600001": -100e4},
    )
    rep = wr.build_daily_war_report(top_n=2, l2=l2, db=wr_db,
                                    now=datetime(2026, 10, 9, 15, 30))
    assert rep["available"] is True
    for k in ("market", "generated_at", "snapshot_date", "universe", "computed",
              "top_inflow", "top_outflow", "industry", "split_wash",
              "caliber", "direction_semantics", "sources", "note"):
        assert k in rep, f"缺少字段 {k}"
    # TOP = 净流入最大 2; BOTTOM = 净流出最大(升序头部)
    assert [r["symbol"] for r in rep["top_inflow"]] == ["000001", "000002"]
    assert [r["symbol"] for r in rep["top_outflow"]] == ["600000", "600001"]
    assert rep["top_inflow"][0]["main_net_wan"] == 500.0
    assert rep["caliber"] == "ths"
    # 缺源子块显式降级(不编造)
    assert rep["industry"]["available"] is False and rep["industry"]["note"]
    assert rep["split_wash"]["available"] is False and rep["split_wash"]["note"]
    # 无采样 → 个股净额变化显式 None
    assert rep["top_inflow"][0]["net_change_wan"] is None


def test_war_report_net_change_from_samples(wr_db, monkeypatch):
    from sqlalchemy import text

    from src.core import war_report as wr

    _patch_subblocks(monkeypatch)
    wr_db.execute(
        text("INSERT INTO dde_minute_flow "
             "(trade_date, market, symbol, sample_ts, cum_net_wan, delta_net_wan, source) "
             "VALUES ('2026-10-09', 'CN', '000001', '10:00', 300.0, 80.0, 'thsdk_dde')"),
    )
    wr_db.commit()
    l2 = FakeL2([("USZA000001", "A")], {"000001": 500e4})
    rep = wr.build_daily_war_report(top_n=1, l2=l2, db=wr_db, now=datetime(2026, 10, 9, 15, 30))
    assert rep["top_inflow"][0]["net_change_wan"] == 80.0
    assert rep["top_inflow"][0]["samples"] == 1


def test_war_report_source_down_explicit_degrade(wr_db, monkeypatch):
    """全市场 DDE 空 → 战报 available=False + note(不落假快照)。"""
    from src.core import war_report as wr

    _patch_subblocks(monkeypatch)

    class EmptyL2:
        def get_stock_cn_lists(self):
            return pd.DataFrame({"代码": [], "名称": []})

        def get_dde_flow(self, *a, **k):
            return pd.DataFrame()

    rep = wr.build_daily_war_report(l2=EmptyL2(), db=wr_db)
    assert rep["available"] is False and rep["note"]


# ────────────────────────── run_war_report_job 落库 ──────────────────────────
def test_war_report_job_stores_snapshot(wr_db, monkeypatch):
    from src.core import war_report as wr
    from src.db.models import WarReportDaily

    _patch_subblocks(monkeypatch)
    l2 = FakeL2([("USZA000001", "A"), ("USZA000002", "B")], {"000001": 500e4, "000002": -100e4})
    out = wr.run_war_report_job(top_n=1, l2=l2, now=datetime(2026, 10, 9, 15, 30))
    assert out["ok"] is True
    assert out["snapshot_date"] == "2026-10-09"
    assert out["report"]["available"] is True
    # 读回: 落库一行
    row = (
        wr_db.query(WarReportDaily)
        .filter(WarReportDaily.snapshot_date == "2026-10-09")
        .first()
    )
    assert row is not None and row.payload["top_inflow"][0]["symbol"] == "000001"


def test_war_report_job_source_down_not_stored(wr_db, monkeypatch):
    from src.core import war_report as wr
    from src.db.models import WarReportDaily

    _patch_subblocks(monkeypatch)

    class EmptyL2:
        def get_stock_cn_lists(self):
            return pd.DataFrame({"代码": [], "名称": []})

        def get_dde_flow(self, *a, **k):
            return pd.DataFrame()

    out = wr.run_war_report_job(l2=EmptyL2(), now=datetime(2026, 10, 9, 15, 30))
    assert out["ok"] is False
    assert wr_db.query(WarReportDaily).count() == 0  # 不把"无数据"存成快照


# ────────────────────────── 端点契约 ──────────────────────────
class _FakeOwner:
    id = "test-owner"
    username = "test-owner"
    role = "owner"
    is_active = True
    token_version = 0


def _make_client(db_rows=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import war_report as wr_api
    from src.web.api.auth import get_current_user, require_owner

    class FakeQuery:
        def __init__(self, rows):
            self._rows = rows

        def filter(self, *a, **k):
            return self

        def order_by(self, *a):
            return self

        def first(self):
            return self._rows[0] if self._rows else None

    class FakeDB:
        def __init__(self, rows):
            self._rows = rows

        def query(self, model):
            return FakeQuery(self._rows)

    app = FastAPI()
    app.include_router(wr_api.router, prefix="/api/war-report")
    app.dependency_overrides[wr_api.get_db] = lambda: FakeDB(db_rows or [])
    app.dependency_overrides[get_current_user] = lambda: _FakeOwner()
    app.dependency_overrides[require_owner] = lambda: _FakeOwner()
    return TestClient(app)


def _snapshot_row(payload):
    class Row:
        snapshot_date = "2026-10-09"
        stock_market = "CN"
        created_at = datetime(2026, 10, 9, 15, 31, 0)

    Row.payload = payload
    return Row()


def test_war_report_daily_no_snapshot_unavailable():
    r = _make_client([]).get("/api/war-report/daily")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False and body["note"]


def test_war_report_daily_snapshot_contract():
    payload = {
        "available": True, "generated_at": "2026-10-09T15:31:00", "caliber": "ths",
        "snapshot_date": "2026-10-09",
        "top_inflow": [{"symbol": "000001", "name": "A", "main_net_wan": 500.0,
                        "net_change_wan": 80.0, "samples": 3, "source": "thsdk_dde"}],
        "top_outflow": [], "universe": 5141, "computed": 5000,
        "industry": {"available": False, "note": "行业源不可用", "rows": []},
        "split_wash": {"available": False, "note": "无 .tck 源", "split_count": None},
    }
    r = _make_client([_snapshot_row(payload)]).get("/api/war-report/daily")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["snapshot_date"] == "2026-10-09"
    assert body["top_inflow"][0]["symbol"] == "000001"
    assert body["industry"]["available"] is False
