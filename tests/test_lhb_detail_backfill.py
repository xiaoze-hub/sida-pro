"""龙虎榜机构/营业部明细(东财, 全自动) — 2026-09-10 老板"切换到东财全自动方案"。

数据源(东财 datacenter, 实测 2026-09-10 可达):
- 机构买卖统计 RPT_ORGANIZATION_TRADE_DETAILS → lhb_institution_daily(上市后 1/2/3/5/10 日涨幅)
- 营业部明细   RPT_BILLBOARD_DAILYDETAILSBUY / ...SELL → lhb_seat_details(席位买卖额/3日上涨概率)

口径: 逐日幂等 upsert; 行唯一键 = 内容哈希 row_uid(席位同日同部同额可重复出现在不同上榜原因,
不能只按 (日期,营业部) 去重); 抓取失败如实记 0 行, 不上报假成功。
"""
from __future__ import annotations

import src.db.session as dbs
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from src.core import lhb_detail_backfill as lhb_detail
from src.web.migrations import _m160_lhb_detail_tables


def _mk_engine():
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        _m160_lhb_detail_tables(conn)
    return eng


def _patch(monkeypatch):
    eng = _mk_engine()
    monkeypatch.setattr(dbs, "engine", eng)
    return eng


INST_ROW = {
    "SECURITY_CODE": "002636",
    "SECURITY_NAME_ABBR": "金安国纪",
    "TRADE_DATE": "2026-09-10 00:00:00",
    "CLOSE_PRICE": 12.34,
    "CHANGE_RATE": 10.02,
    "BUY_TIMES": 2,
    "SELL_TIMES": 1,
    "BUY_AMT": 288_000_000.0,
    "SELL_AMT": 148_000_000.0,
    "NET_BUY_AMT": 140_000_000.0,
    "ACCUM_AMOUNT": 1_200_000_000.0,
    "RATIO": 2.92,
    "TURNOVERRATE": 12.5,
    "FREECAP": 3.3e10,
    "EXPLANATION": "连续三个交易日内涨幅偏离值累计达20%的证券",
    "D1_CLOSE_ADJCHRATE": 1.1,
    "D2_CLOSE_ADJCHRATE": -2.2,
    "D3_CLOSE_ADJCHRATE": 3.3,
    "D5_CLOSE_ADJCHRATE": 5.5,
    "D10_CLOSE_ADJCHRATE": 9.9,
}

SEAT_ROW = {
    "SECURITY_CODE": "002636",
    "TRADE_DATE": "2026-09-10 00:00:00",
    "OPERATEDEPT_CODE": "10456710",
    "OPERATEDEPT_NAME": "深股通专用",
    "EXPLANATION": "连续三个交易日内涨幅偏离值累计达20%的证券",
    "CHANGE_RATE": 10.02,
    "CLOSE_PRICE": 12.34,
    "ACCUM_AMOUNT": 1_200_000_000.0,
    "ACCUM_VOLUME": 99_000_000.0,
    "BUY": 288_000_000.0,
    "SELL": 148_000_000.0,
    "NET": 140_000_000.0,
    "RISE_PROBABILITY_3DAY": 49.33,
    "CHANGE_TYPE": "137001002001002",
    "TRADE_ID": "100410465",
    "TOTAL_BUYRIO": 2.92,
    "TOTAL_SELLRIO": 1.51,
}


def test_norm_institutions_maps_fields_and_drops_bad_symbols():
    rows = lhb_detail._norm_institutions([INST_ROW, {"SECURITY_CODE": "BAD", "TRADE_DATE": "2026-09-10"}])
    assert len(rows) == 1
    r = rows[0]
    assert r["trade_date"] == "20260910"
    assert r["symbol"] == "002636" and r["name"] == "金安国纪"
    assert r["buy_times"] == 2 and r["sell_times"] == 1
    assert r["net_amt"] == 140_000_000.0 and r["ratio"] == 2.92
    assert r["d1_pct"] == 1.1 and r["d10_pct"] == 9.9
    assert r["reason"].startswith("连续三个交易日")


def test_norm_seats_side_and_row_uid_stable():
    a = lhb_detail._norm_seats([SEAT_ROW], "buy")[0]
    assert a["side"] == "buy" and a["trade_id"] == "100410465"
    assert a["operate_dept_name"] == "深股通专用"
    assert a["net_amt"] == 140_000_000.0
    assert a["rise_probability_3d"] == 49.33
    # 同内容两次归一 → row_uid 相同(幂等键); 卖出侧不同侧 → 不同 uid
    b = lhb_detail._norm_seats([SEAT_ROW], "buy")[0]
    c = lhb_detail._norm_seats([SEAT_ROW], "sell")[0]
    assert a["row_uid"] == b["row_uid"]
    assert a["row_uid"] != c["row_uid"]


def test_sync_dates_is_idempotent(monkeypatch):
    _patch(monkeypatch)
    monkeypatch.setattr(lhb_detail, "_fetch_institutions", lambda d: [dict(INST_ROW)])
    monkeypatch.setattr(lhb_detail, "_fetch_seats", lambda d, side: [dict(SEAT_ROW)] if side == "buy" else [])

    first = lhb_detail.sync_dates(["20260910"])
    second = lhb_detail.sync_dates(["20260910"])
    assert first["institutions"] == 1 and first["seats"] == 1
    assert second["institutions"] == 1 and second["seats"] == 1  # 重拉仍 1 行(幂等, 不重复插入)

    eng = dbs.engine
    with eng.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM lhb_institution_daily")).scalar() == 1
        assert conn.execute(text("SELECT COUNT(*) FROM lhb_seat_details")).scalar() == 1


def test_daily_job_never_raises_on_fetch_error(monkeypatch):
    _patch(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("eastmoney down")

    monkeypatch.setattr(lhb_detail, "_fetch_institutions", _boom)
    monkeypatch.setattr(lhb_detail, "_fetch_seats", _boom)
    out = lhb_detail.daily_job(days=1)
    assert out["ok"] is False
    assert out["errors"] >= 1
