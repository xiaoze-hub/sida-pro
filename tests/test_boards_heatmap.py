"""板块热力图端点 /api/boards/heatmap: 一次拉全板块最新日线(treemap 数据源)。

口径(2026-09-10, P1-1 借鉴 OpenTerminal 板块热力图):
- 每板块取其 BoardDaily 最新一行(逐板块 latest, 非全局同日);
- Boards 表存在但无日线的板块也返回(has_daily=False + 指标 None,
  前端显灰块"无数据" —— 不编造数字, 见 AGENTS.md 数据缺失显式标注);
- trade_date = 有日线项中的最新日期(UI 基准日标注用), 全空为 None。
"""

from __future__ import annotations

from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.web.api.boards as boards_api
from src.web import models as M  # noqa: F401  确保模型注册到 Base.metadata
from src.web.database import Base, get_db
from src.web.models import Board, BoardDaily


def _client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(boards_api.router, prefix="/api/boards")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    return TestClient(app), Session


def test_heatmap_returns_latest_per_board():
    """同板块多日线 → 只回最新一行; trade_date 取最新日期。"""
    client, Session = _client()
    db = Session()
    db.add(Board(block_code="URFI0001", name="半导体", board_type="industry"))
    db.add(BoardDaily(block_code="URFI0001", date=date(2026, 9, 9), change_pct=1.5, fund_net=100.0, volume=1000.0))
    db.add(BoardDaily(block_code="URFI0001", date=date(2026, 9, 10), change_pct=2.5, fund_net=200.0, volume=2000.0))
    db.commit()
    db.close()

    resp = client.get("/api/boards/heatmap?source=ths&type=industry")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trade_date"] == "2026-09-10"
    assert body["count"] == 1
    it = body["items"][0]
    assert it["block_code"] == "URFI0001"
    assert it["name"] == "半导体"
    assert it["board_type"] == "industry"
    assert it["change_pct"] == 2.5
    assert it["fund_net"] == 200.0
    assert it["volume"] == 2000.0
    assert it["date"] == "2026-09-10"
    assert it["has_daily"] is True


def test_heatmap_keeps_older_latest_for_stale_board():
    """板块 A 无当日线(仅昨天) → 仍返回自己的最新一行(不因全局最新日丢板块)。"""
    client, Session = _client()
    db = Session()
    db.add(Board(block_code="URFI0001", name="半导体", board_type="industry"))
    db.add(Board(block_code="URFI0002", name="银行", board_type="industry"))
    db.add(BoardDaily(block_code="URFI0001", date=date(2026, 9, 10), change_pct=2.5))
    db.add(BoardDaily(block_code="URFI0002", date=date(2026, 9, 8), change_pct=-1.2))
    db.commit()
    db.close()

    resp = client.get("/api/boards/heatmap?source=ths&type=industry")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 2
    by_code = {i["block_code"]: i for i in body["items"]}
    assert by_code["URFI0002"]["date"] == "2026-09-08"
    assert by_code["URFI0002"]["change_pct"] == -1.2
    # trade_date 是全局最新(供 UI 标注基准日), 单项 date 保留各自口径
    assert body["trade_date"] == "2026-09-10"


def test_heatmap_includes_boards_without_daily_as_nodata():
    """Boards 有注册但无日线 → has_daily=False 且指标为 None(前端显式标注无数据)。"""
    client, Session = _client()
    db = Session()
    db.add(Board(block_code="URFI0001", name="半导体", board_type="industry"))
    db.add(Board(block_code="URFI0003", name="新概念", board_type="industry"))
    db.add(BoardDaily(block_code="URFI0001", date=date(2026, 9, 10), change_pct=2.5))
    db.commit()
    db.close()

    resp = client.get("/api/boards/heatmap?source=ths&type=industry")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 2
    nd = next(i for i in body["items"] if i["block_code"] == "URFI0003")
    assert nd["has_daily"] is False
    assert nd["change_pct"] is None
    assert nd["date"] is None
    assert nd["name"] == "新概念"


def test_heatmap_filters_by_type_and_rejects_invalid():
    """type=concept 只回概念板块; 非法 type → 400。"""
    client, Session = _client()
    db = Session()
    db.add(Board(block_code="URFI0001", name="半导体", board_type="industry"))
    db.add(Board(block_code="URFI9001", name="AI算力", board_type="concept"))
    db.add(BoardDaily(block_code="URFI0001", date=date(2026, 9, 10), change_pct=2.5))
    db.add(BoardDaily(block_code="URFI9001", date=date(2026, 9, 10), change_pct=-3.0))
    db.commit()
    db.close()

    resp = client.get("/api/boards/heatmap?source=ths&type=concept")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [i["block_code"] for i in body["items"]] == ["URFI9001"]

    bad = client.get("/api/boards/heatmap?source=ths&type=foo")
    assert bad.status_code == 400


def test_heatmap_sorted_by_change_pct_desc_nulls_last():
    """排序: 涨跌幅降序, 无数据排最后(前端按序渲染兜底; 色块布局不受影响)。"""
    client, Session = _client()
    db = Session()
    for code, name, chg in [
        ("URFI0001", "A", 2.0),
        ("URFI0002", "B", -1.0),
        ("URFI0003", "C", None),
    ]:
        db.add(Board(block_code=code, name=name, board_type="industry"))
        if chg is not None:
            db.add(BoardDaily(block_code=code, date=date(2026, 9, 10), change_pct=chg))
    db.commit()
    db.close()

    resp = client.get("/api/boards/heatmap?source=ths&type=industry")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [i["name"] for i in body["items"]] == ["A", "B", "C"]
    assert body["count"] == 3


def test_heatmap_empty_db_returns_empty():
    """空库 → items=[], trade_date=None, count=0(UI 空态走"等待每日同步"文案)。"""
    client, Session = _client()
    resp = client.get("/api/boards/heatmap?source=ths&type=industry")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["trade_date"] is None
    assert body["count"] == 0
    Session().close()
