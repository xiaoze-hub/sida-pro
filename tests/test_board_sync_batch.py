"""板块同步批量取数修复回归 (2026-09-10 生产实撞)。

背景: `sync_boards_to_db` 原逐板块调 thsdk "基础数据" 档 —— 实测该档无涨跌幅字段,
change_pct 恒 None → 480 板块全量被跳过, `board_daily` 长期 0 行(热力图/轮动全空)。
修法: 批量两档查询 —— "扩展"(涨幅/主力净流入, 元) + "基础数据"(总金额=成交额, 元);
全零(无行情概念)视为无数据跳过, 保持"无数据"语义。

本文件纯 mock、无网络(进 CI 离线门禁; 真实接口路径由 test_thsdk_board.py 网络用例覆盖)。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.core.thsdk_board as tb
import src.web.models  # noqa: F401  注册 ORM 模型
from src.web.database import Base


def _mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _batch_client(ext_df: pd.DataFrame | None = None, base_df: pd.DataFrame | None = None) -> MagicMock:
    """fake 客户端: get_block_market_batch(codes, mode) → 对应档位 records。"""
    client = MagicMock()

    def _batch(codes, extended="扩展"):
        df = ext_df if extended == "扩展" else base_df
        if df is None or df.empty:
            return []
        return df[df["代码"].isin(list(codes))].to_dict(orient="records")

    client.get_block_market_batch.side_effect = _batch
    return client


def _ext_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "代码": ["URFI881132", "URFI881155"],
            "涨幅": [-0.3679, 1.4658],
            "主力净流入": [-26459485, 216177500],
        }
    )


def _base_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "代码": ["URFI881132", "URFI881155"],
            "总金额": [1445759700, 27829347000],
        }
    )


@pytest.fixture(autouse=True)
def _clear_board_cache():
    tb.clear_cache()
    yield
    tb.clear_cache()


# ---------------------------------------------------------------------------
# 1) fetch_block_snapshots: 两档合并 + 分片
# ---------------------------------------------------------------------------
def test_fetch_block_snapshots_merges_two_modes(monkeypatch):
    monkeypatch.setattr(tb, "_client", lambda: _batch_client(_ext_df(), _base_df()))
    snaps = tb.fetch_block_snapshots(["URFI881132", "URFI881155"])
    assert set(snaps) == {"URFI881132", "URFI881155"}
    a = snaps["URFI881132"]
    assert a["change_pct"] == -0.3679
    assert a["fund_net"] == -26459485
    assert a["volume"] == 1445759700
    assert snaps["URFI881155"]["change_pct"] == 1.4658


def test_fetch_block_snapshots_chunks_codes(monkeypatch):
    client = _batch_client(_ext_df(), _base_df())
    monkeypatch.setattr(tb, "_client", lambda: client)
    codes = [f"URFI{i:06d}" for i in range(200)]
    tb.fetch_block_snapshots(codes)
    # 200 码按 _SNAPSHOT_BATCH 分片 × 2 档
    expected = 2 * ((200 + tb._SNAPSHOT_BATCH - 1) // tb._SNAPSHOT_BATCH)
    assert client.get_block_market_batch.call_count == expected


def test_fetch_block_snapshots_failure_returns_empty(monkeypatch):
    client = MagicMock()
    client.get_block_market_batch.side_effect = RuntimeError("thsdk down")
    monkeypatch.setattr(tb, "_client", lambda: client)
    assert tb.fetch_block_snapshots(["URFI881132"]) == {}


# ---------------------------------------------------------------------------
# 2) 提取器: 总金额=成交额(量能)
# ---------------------------------------------------------------------------
def test_extract_block_metrics_prefers_total_amount():
    m = tb._extract_block_metrics({"总金额": 1445759700, "成交量": 145811370})
    assert m["volume"] == 1445759700


# ---------------------------------------------------------------------------
# 3) sync: 批量写入 + 幂等 + 无数据跳过
# ---------------------------------------------------------------------------
def _patch_lists(monkeypatch, codes=("URFI881132",)):
    monkeypatch.setattr(
        tb, "fetch_ths_industry", lambda: [{"block_code": c, "name": f"行业{c[-3:]}", "board_type": "industry"} for c in codes]
    )
    monkeypatch.setattr(tb, "fetch_ths_concept", lambda: [])


def test_sync_writes_daily_from_batch(monkeypatch):
    db = _mem_db()
    _patch_lists(monkeypatch)
    monkeypatch.setattr(
        tb,
        "fetch_block_snapshots",
        lambda codes: {"URFI881132": {"change_pct": -0.3679, "fund_net": -26459485, "volume": 1445759700}},
    )
    try:
        s1 = tb.sync_boards_to_db(db=db)
        assert s1["daily_rows"] == 1 and s1["skipped"] == 0
        from src.web.models import BoardDaily

        row = db.query(BoardDaily).filter(BoardDaily.block_code == "URFI881132").first()
        assert row.change_pct == -0.3679 and row.fund_net == -26459485 and row.volume == 1445759700

        s2 = tb.sync_boards_to_db(db=db)
        assert s2["daily_rows"] == 1
        assert db.query(BoardDaily).count() == 1  # 幂等: 同日更新不重复插入
    finally:
        db.close()


def test_sync_skips_all_zero_snapshot(monkeypatch):
    """全零(概念无行情)= 无数据 → 不写日线, 保持"无数据"语义(热力图显灰不显平盘)。"""
    db = _mem_db()
    _patch_lists(monkeypatch, codes=("URFI301713",))
    monkeypatch.setattr(
        tb, "fetch_block_snapshots",
        lambda codes: {"URFI301713": {"change_pct": 0.0, "fund_net": 0, "volume": 0}},
    )
    try:
        s = tb.sync_boards_to_db(db=db)
        assert s["daily_rows"] == 0 and s["skipped"] == 1
        from src.web.models import BoardDaily

        assert db.query(BoardDaily).count() == 0
    finally:
        db.close()


def test_sync_missing_snapshot_is_skipped(monkeypatch):
    db = _mem_db()
    _patch_lists(monkeypatch, codes=("URFI881132",))
    monkeypatch.setattr(tb, "fetch_block_snapshots", lambda codes: {})
    try:
        s = tb.sync_boards_to_db(db=db)
        assert s["daily_rows"] == 0 and s["skipped"] == 1
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4) 计划改到收盘后(16:10): 日线=当日收盘口径
# ---------------------------------------------------------------------------
def test_cron_runs_after_close():
    assert tb.BOARD_SYNC_CRON["hour"] >= 15
