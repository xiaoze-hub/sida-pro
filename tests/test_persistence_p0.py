# -*- coding: utf-8 -*-
"""P0 落库加固验收测试(2026-10-18)。

覆盖《全量数据落库排查与持久化加固方案_20261018.md》§8 的批次 1(P0-a)+ 批次 2(P0-b):

  ① 迁移 v201~v205 在 SQLite 上可建表、可重跑(幂等)、模型列与 DDL 一致;
  ② 竞价过程帧: append-only 幂等 + 逐分钟回放 + 保留期清理 + 窗口外自守卫;
  ③ 悟道竞价: 原始 JSON → 结构化行, 不可用日**不写行**(显式空);
  ④ 可转债: 条款版本化 + 读库优先(零 RPC) + 当日快照读穿透;
  ⑤ more_info: 104 字段映射 + 零值不落库 + 最近交易日回退;
  ⑥ 指数日K: prev_close 链 + spark 读库优先(0 次 vendor 调用)。

守的是方案 §8 的统一门禁: "新表有写入者单测(mock vendor) + 读侧降级单测"。
"""
from __future__ import annotations

import inspect
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy import inspect as sqla_inspect
from sqlalchemy.orm import sessionmaker

import src.db.models  # noqa: F401 - 注册 ORM 模型
from src.core import auction_tick_sampler as ats
from src.core import auction_wudao_store as aws
from src.core import cb_radar
from src.core import cb_radar_store as cbs
from src.core import more_info_daily as mid
from src.collectors import index_daily as idxd
from src.db.models import (
    AuctionTickSnapshot,
    AuctionWudaoDaily,
    CbDailySnapshot,
    CbTerms,
    IndexDaily,
    MoreInfoDaily,
)
from src.db.session import Base
from src.web import migrations as mig

NEW_MODELS = (
    AuctionTickSnapshot,
    AuctionWudaoDaily,
    CbTerms,
    CbDailySnapshot,
    MoreInfoDaily,
    IndexDaily,
)
P0_MIGRATIONS = (
    (201, "_m201_auction_tick_snapshots"),
    (202, "_m202_auction_wudao_daily"),
    (203, "_m203_cb_tables"),
    (204, "_m204_more_info_daily"),
    (205, "_m205_index_daily"),
)


@pytest.fixture()
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/p0.db")
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def maker(engine):
    return sessionmaker(bind=engine)


# ═══════════════════════════════════════════════════════════════════════════
# ① 迁移 / 模型
# ═══════════════════════════════════════════════════════════════════════════
def test_migrations_201_205_create_tables_and_are_idempotent(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/mig.db")
    for version, fn_name in P0_MIGRATIONS:
        runner = getattr(mig, fn_name)
        assert runner.__name__.startswith(f"_m{version}_")
        with eng.begin() as conn:
            runner(conn)
            runner(conn)  # 重跑必须幂等(启动会重复执行)
    insp = sqla_inspect(eng)
    tables = set(insp.get_table_names())
    for model in NEW_MODELS:
        assert model.__tablename__ in tables, f"{model.__tablename__} 未建表"
        assert {c["name"] for c in insp.get_columns(model.__tablename__)} == set(
            model.__table__.c.keys()
        ), f"{model.__tablename__} 模型列与迁移 DDL 不一致"
    eng.dispose()


def test_migrations_registry_tail_is_contiguous():
    versions = [m.version for m in mig.MIGRATIONS]
    assert versions[:1] == [101]
    assert versions[-5:] == [201, 202, 203, 204, 205]
    for prev, cur in zip(versions, versions[1:]):
        assert cur == prev + 1, f"版本不连续: {prev} -> {cur}"


def test_cb_terms_has_14_business_fields():
    business = set(CbTerms.__table__.c.keys()) - {"id", "source", "captured_at", "raw"}
    assert len(business) == 14, sorted(business)


def test_unique_keys_protect_idempotency():
    def uniques(model):
        return {
            frozenset(c.name for c in con.columns)
            for con in model.__table__.constraints
            if con.__class__.__name__ == "UniqueConstraint"
        }

    assert frozenset({"trade_date", "symbol", "market", "tick_ts", "source"}) in uniques(
        AuctionTickSnapshot
    )
    assert frozenset({"code", "as_of"}) in uniques(CbTerms)
    assert frozenset({"trade_date", "code"}) in uniques(CbDailySnapshot)
    assert frozenset({"trade_date", "symbol", "market"}) in uniques(MoreInfoDaily)
    assert frozenset({"trade_date", "code"}) in uniques(IndexDaily)


# ═══════════════════════════════════════════════════════════════════════════
# ② 竞价过程帧
# ═══════════════════════════════════════════════════════════════════════════
def _snap(**over):
    base = {
        "last_close": 10.0,
        "open": 10.6,
        "vol": 12000.0,
        "buy1": 10.6,
        "buy1_vol": 9000.0,
        "sell1": 10.61,
        "sell1_vol": 3000.0,
        "open_pct": 6.0,
        "open_amount": 12720.0,
        "open_limit_buy": 0.0,
        "cancel_buy": 800.0,
        "cancel_sell": 400.0,
    }
    base.update(over)
    return base


def test_frame_from_snapshot_derives_process_fields():
    f = ats.frame_from_snapshot("600519", _snap(), tick_ts="09:20:00")
    assert f["time_bucket"] == "09:20"
    assert f["ref_price"] == pytest.approx(10.6)
    assert f["unmatched_vol"] == pytest.approx(6000.0)
    assert f["cancel_cum"] == pytest.approx(1200.0)
    # 1200 / (1200 + 12000)
    assert f["withdraw_rate"] == pytest.approx(1200 / 13200, abs=1e-4)
    assert f["verdict"] == "strong_open"
    assert f["open_pct"] == pytest.approx(6.0)


def test_frame_from_snapshot_is_honest_about_missing_inputs():
    f = ats.frame_from_snapshot(
        "002361", {"last_close": 10.0, "open": 9.0}, tick_ts="09:16:00"
    )
    assert f["unmatched_vol"] is None          # 没有一档量 → 不硬凑 0
    assert f["cancel_cum"] is None
    assert f["withdraw_rate"] is None
    assert f["open_pct"] == pytest.approx(-10.0)   # 由价格算, 可复现
    assert f["verdict"] == "limit_down_open"

    assert ats.frame_from_snapshot("002361", {}, tick_ts="09:16:00") is None


def test_upsert_frames_is_idempotent_per_frame(engine, maker):
    with maker() as s:
        f1 = ats.frame_from_snapshot("600519", _snap(), tick_ts="09:16:00")
        f1["trade_date"] = "2026-10-16"
        f2 = ats.frame_from_snapshot(
            "600519", _snap(open=10.8, open_pct=8.0), tick_ts="09:17:00"
        )
        f2["trade_date"] = "2026-10-16"
        assert ats.upsert_frames([f1, f2], session=s) == 2

        f1b = ats.frame_from_snapshot("600519", _snap(open=10.7), tick_ts="09:16:00")
        f1b["trade_date"] = "2026-10-16"
        assert ats.upsert_frames([f1b], session=s) == 1

    insp = sqla_inspect(engine)
    rows = insp.get_table_names()
    assert "auction_tick_snapshots" in rows
    with maker() as s:
        frames = ats.load_frames("2026-10-16", symbol="600519", session=s)
    assert [f["tick_ts"] for f in frames] == ["09:16:00", "09:17:00"]
    assert frames[0]["ref_price"] == pytest.approx(10.7)   # 同帧覆盖为最新


def test_replay_reports_sequence_and_degrades_when_empty(maker):
    with maker() as s:
        for ts, px in (("09:15:00", 10.2), ("09:20:00", 10.5), ("09:25:00", 10.9)):
            f = ats.frame_from_snapshot(
                "600519", _snap(open=px, open_pct=(px / 10 - 1) * 100), tick_ts=ts
            )
            f["trade_date"] = "2026-10-16"
            ats.upsert_frames([f], session=s)

    out = ats.replay("2026-10-16", "600519", session_factory=maker)
    assert out["available"] is True
    assert out["frame_count"] == 3
    assert out["first_ts"] == "09:15:00" and out["last_ts"] == "09:25:00"
    assert out["price_change_pct"] == pytest.approx((10.9 / 10.2 - 1) * 100, abs=0.01)

    empty = ats.replay("2026-10-17", "600519", session_factory=maker)
    assert empty["available"] is False and empty["frames"] == []


def test_purge_old_frames_keeps_retention_window(maker):
    with maker() as s:
        for day in ("2025-01-01", "2026-10-15", "2026-10-16"):
            f = ats.frame_from_snapshot("600519", _snap(), tick_ts="09:16:00")
            f["trade_date"] = day
            ats.upsert_frames([f], session=s)
        removed = ats.purge_old_frames(keep_days=30, today="2026-10-16", session=s)
        left = s.scalars(select(AuctionTickSnapshot.trade_date)).all()
    assert removed == 1
    assert sorted(set(left)) == ["2026-10-15", "2026-10-16"]


def test_sample_once_persists_via_injected_fetcher(maker, monkeypatch):
    monkeypatch.setattr(
        ats, "upsert_frames",
        lambda frames, trade_date=None, session=None, session_factory=None: (
            _count_and_persist(frames, trade_date, maker)
        ),
    )
    out = ats.sample_once(
        symbols=["600519", "000001"],
        now=datetime(2026, 10, 16, 9, 20, 0),
        fetcher=lambda syms: {"600519": _snap(), "000001": {}},
    )
    assert out["ok"] is True
    assert out["frames"] == 1                  # 无价格的票不产帧
    assert out["persisted"] == 1


def _count_and_persist(frames, trade_date, maker):
    with maker() as s:
        s.add(AuctionTickSnapshot(**{**frames[0], "trade_date": trade_date}))
        s.commit()
    return len(frames)


def test_auction_job_skips_outside_window_without_writing(maker, monkeypatch):
    called = {"n": 0}
    out = ats.run_auction_tick_job(
        now=datetime(2026, 10, 16, 12, 0, 0),
        fetcher=lambda syms: called.__setitem__("n", called["n"] + 1) or {},
    )
    assert out["ok"] is True and out["skipped"] == "outside_auction_window"
    assert called["n"] == 0

    weekend = ats.run_auction_tick_job(now=datetime(2026, 10, 18, 9, 20, 0))
    assert weekend["skipped"] == "non_trading_day"


# ═══════════════════════════════════════════════════════════════════════════
# ③ 悟道竞价归档
# ═══════════════════════════════════════════════════════════════════════════
_WUDAO_RAW = {
    "opening_snapshot": {
        "data": {
            "topBidAmount": [
                {"code": "600519", "name": "贵州茅台", "consistency": 0.91,
                 "bidStrength": 88.0, "openPct": 3.2},
            ],
            "summary": {"limitUpOpen": 1},
        }
    },
    "weak_to_strong": {
        "rows": [
            {"stockCode": "300750.SZ", "stockName": "宁德时代", "wtsScore": 9.1},
            {"code": "600519", "bidStrength": 88.0},   # 重复票, 应合并而非新增
        ]
    },
    "theme_strength": {"data": [{"themeName": "机器人", "themeStrength": 7.5}]},
    "limited": False,
    "error": "",
}


def test_extract_rows_normalizes_and_dedupes():
    rows = {r["symbol"]: r for r in aws.extract_rows(_WUDAO_RAW)}
    assert set(rows) == {"600519", "300750"}
    assert rows["600519"]["consistency"] == pytest.approx(0.91)
    assert rows["600519"]["bid_strength"] == pytest.approx(88.0)
    assert rows["600519"]["open_pct"] == pytest.approx(3.2)
    assert rows["300750"]["weak_to_strong"] is True     # 来自 weak_to_strong 段
    assert rows["300750"]["name"] == "宁德时代"


def test_extract_rows_returns_empty_on_unknown_shape():
    assert aws.extract_rows({}) == []
    assert aws.extract_rows({"opening_snapshot": {"text": "只有中文文本"}}) == []


def test_wudao_upsert_idempotent_and_load(maker):
    rows = aws.extract_rows(_WUDAO_RAW)
    with maker() as s:
        assert aws.upsert_daily(rows, trade_date="2026-10-16", session=s) == 2
        assert aws.upsert_daily(rows, trade_date="2026-10-16", session=s) == 2

    out = aws.load_daily("2026-10-16", session_factory=maker)
    assert out["available"] is True and len(out["rows"]) == 2
    assert {r["symbol"] for r in out["rows"]} == {"600519", "300750"}

    empty = aws.load_daily("2026-10-17", session_factory=maker)
    assert empty["available"] is False and empty["rows"] == []


def test_wudao_capture_persists_when_available_and_writes_nothing_when_not(maker):
    ok = aws.capture_daily(
        now=datetime(2026, 10, 16, 9, 26, 0),
        fetcher=lambda: _WUDAO_RAW,
        session_factory=maker,
    )
    assert ok["ok"] is True and ok["rows"] == 2
    with maker() as s:
        assert len(s.scalars(select(AuctionWudaoDaily)).all()) == 2

    # 限流窗口 = 已知不可用(非错误) → skip, 不判 failed
    limited = aws.capture_daily(
        now=datetime(2026, 10, 17, 9, 26, 0),
        fetcher=lambda: {"opening_snapshot": {}, "limited": True,
                         "error": "悟道限流窗口(9:15-10:30)"},
        session_factory=maker,
    )
    assert limited["ok"] is True and limited["skipped"] == "wudao_rate_limit_window"
    assert limited["rows"] == 0

    # 真错误(未配 token / 网关挂) → ok=False, 作业面板可见
    broken = aws.capture_daily(
        now=datetime(2026, 10, 17, 9, 26, 0),
        fetcher=lambda: {"opening_snapshot": {}, "limited": False,
                         "error": "connect timeout"},
        session_factory=maker,
    )
    assert broken["ok"] is False and "connect timeout" in broken["reason"]
    with maker() as s:
        assert len(s.scalars(select(AuctionWudaoDaily)).all()) == 2   # 不写空行


# ═══════════════════════════════════════════════════════════════════════════
# ④ 可转债
# ═══════════════════════════════════════════════════════════════════════════
_TERM_RAW = {
    "KZZCode": "113001.SH",
    "KZZName": "转债一号",
    "KZZNow": 118.5,
    "HSCode": "600519.SH",
    "ZGPrice": 20.0,
    "ForceRedeem": 26.0,
    "RealValue": 92.3,
    "HSScore": "AA+",
    "ZGDate": "20250101",
    "EndDate": "20300101",
    "PutBack": 15.0,
    "RestScope": 8.5,
    "CurRate": 1.2,
}


def _row(**over):
    base = {
        "code": "113001",
        "name": "转债一号",
        "stock_code": "600519",
        "bond_price": 118.5,
        "bond_prev_close": 117.0,
        "stock_price": 21.0,
        "stock_prev_close": 20.5,
        "conv_price": 20.0,
        "conv_value": 105.0,
        "premium_pct": 12.86,
        "double_low": 131.36,
        "redeem_distance_pct": -19.23,
        "pure_bond_premium_pct": 28.39,
        "scenario_keys": ["force_redeem_near"],
        "scenarios": [{"key": "force_redeem_near", "label": "接近强赎"}],
    }
    base.update(over)
    return base


def test_term_columns_map_14_fields():
    cols = cbs.term_columns("113001", _TERM_RAW, as_of="2026-10-16")
    assert cols["name"] == "转债一号"
    assert cols["stock_code"] == "600519"
    assert cols["conv_price"] == pytest.approx(20.0)
    assert cols["force_redeem"] == pytest.approx(26.0)
    assert cols["pure_bond_value"] == pytest.approx(92.3)
    assert cols["rating"] == "AA+"
    assert cols["conv_start"] == "2025-01-01"
    assert cols["maturity_date"] == "2030-01-01"
    assert cols["put_back_price"] == pytest.approx(15.0)
    assert cols["rest_scope"] == pytest.approx(8.5)
    assert cols["cur_rate"] == pytest.approx(1.2)
    assert cols["list_date"] is None            # 客户端未给 → NULL, 不猜


def test_terms_upsert_versions_and_latest_wins(maker):
    with maker() as s:
        cbs.upsert_terms(s, "113001", _TERM_RAW, as_of="2026-10-01")
        cbs.upsert_terms(s, "113001", {**_TERM_RAW, "ZGPrice": 18.0}, as_of="2026-10-16")
        assert cbs.upsert_terms(s, "113001", {}, as_of="2026-10-17") is False

    with maker() as s:
        got = cbs.load_terms(s, ["113001"])["113001"]
        assert got["ZGPrice"] == pytest.approx(18.0)
        assert got["_as_of"] == "2026-10-16"
        assert got["_stock_code"] == "600519"
        assert cbs.terms_are_fresh(got) is True

    assert cbs.terms_are_fresh({"_as_of": "2020-01-01"}) is False


def test_cb_daily_rows_skip_unpriced_and_keep_rank(maker):
    with maker() as s:
        n = cbs.upsert_daily_rows(
            s,
            [_row(), _row(code="123456", bond_price=None)],
            trade_date="2026-10-16",
            universe=500,
        )
        assert n == 1                        # 无债价的行不落库
        n2 = cbs.upsert_daily_rows(
            s, [_row(premium_pct=9.9)], trade_date="2026-10-16", universe=500
        )
        assert n2 == 1

    out = cbs.load_daily_rows(maker(), limit=10)
    assert out["trade_date"] == "2026-10-16"
    assert out["universe"] == 500
    assert len(out["rows"]) == 1
    assert out["rows"][0]["premium_pct"] == pytest.approx(9.9)   # 同日覆盖


def _radar_payload(day: str, n: int = 2) -> dict:
    rows = [_row(code=f"11300{i}", double_low=130 + i) for i in range(n)]
    return {
        "available": True,
        "data_date": day,
        "is_live": True,
        "is_today": True,
        "trade_phase": "continuous",
        "phase_label": "连续竞价",
        "as_of": "2026-10-16T10:00:00+08:00",
        "source": "通达信 TQ",
        "caliber": "原始字段",
        "universe": 480,
        "evaluated": n,
        "limit": n,
        "rows": rows,
        "scenario_counts": {"force_redeem_near": n},
        "note": None,
    }


def test_get_radar_live_then_db_hit(maker):
    calls = {"n": 0}
    day = "2026-10-16"

    def builder(*, limit, now=None):
        calls["n"] += 1
        return _radar_payload(day)

    first = cb_radar.get_radar(
        limit=10, now=datetime(2026, 10, 16, 10, 0, 0),
        builder=builder, session_factory=maker,
    )
    assert first["cache"]["source"] == "live"
    assert first["persisted_rows"] == 2
    assert calls["n"] == 1

    second = cb_radar.get_radar(
        limit=10, now=datetime(2026, 10, 16, 10, 1, 0),
        builder=builder, session_factory=maker,
    )
    assert second["cache"]["source"] == "db"
    assert second["cached"] is True
    assert calls["n"] == 1                     # 读库命中 → 不再回源
    assert second["universe"] == 480
    assert len(second["rows"]) == 2


def test_get_radar_ignores_db_rows_from_previous_day_intraday(maker):
    with maker() as s:
        cbs.upsert_daily_rows(s, [_row()], trade_date="2026-10-15", universe=400)
    out = cb_radar.get_radar(
        limit=10,
        now=datetime(2026, 10, 16, 10, 0, 0),   # 连续竞价中
        builder=lambda *, limit, now=None: _radar_payload("2026-10-16"),
        session_factory=maker,
    )
    assert out["cache"]["source"] == "live"     # 盘中不拿昨收冒充
    assert out["data_date"] == "2026-10-16"


@pytest.fixture()
def clean_global_cb_terms():
    """清掉本文件写进**全局测试库**的条款行, 避免污染后续 cb_radar 用例。"""
    from sqlalchemy import delete

    from src.db.session import SessionLocal

    def _clean():
        try:
            with SessionLocal() as s:
                s.execute(delete(CbTerms).where(CbTerms.code.in_(["113001", "123456"])))
                s.commit()
        except Exception:  # noqa: BLE001 - 表不存在时忽略
            pass
        cb_radar.clear_terms_cache()

    _clean()
    yield
    _clean()


def test_terms_read_through_hits_db_without_rpc(maker, monkeypatch, clean_global_cb_terms):
    from src.db.session import SessionLocal

    today = __import__("datetime").date.today().isoformat()
    with SessionLocal() as s:
        cbs.upsert_terms(s, "113001", _TERM_RAW, as_of=today)

    def _boom(*a, **kw):
        raise AssertionError("命中 cb_terms 时不应调用 kzz_info")

    monkeypatch.setattr("marketdata.vendors.tq.kzz_info", _boom)
    cb_radar.clear_terms_cache()
    got = cb_radar._terms("113001")
    assert got["ZGPrice"] == pytest.approx(20.0)
    assert got["_as_of"] == today


# ═══════════════════════════════════════════════════════════════════════════
# ⑤ more_info 收盘定稿
# ═══════════════════════════════════════════════════════════════════════════
def _mi(**over):
    base = {
        "symbol": "600519",
        "market": "CN",
        "turnover_rate": 1.2,
        "volume_ratio": 0.9,
        "commission_ratio": 12.5,
        "total_market_value": 21000.0,
        "circulating_market_value": 21000.0,
        "change_pct": 3.1,
        "change_pct_5d": 5.0,
        "change_pct_20d": 8.0,
        "change_pct_ytd": 12.0,
        "limit_up_amount": 0.0,
        "limit_up_ratio": 0.0,
        "open_amount": 1500.0,
        "open_limit_buy": 0.0,
        "consecutive_limit_days": 0,
        "consecutive_up_days": 2,
        "pe_dynamic": 30.0,
        "pe_ttm": 28.0,
        "pb": 9.0,
        "dividend_yield": 1.5,
        "beta": 1.1,
        "l2_tick_num": 12345,
        "l2_order_num": 678,
        "total_buy_vol": 1000.0,
        "total_sell_vol": 900.0,
        "cancel_buy": 10.0,
        "cancel_sell": 20.0,
        "zjl": 300.0,
        "zjl_hb": 500.0,
        "raw": {"OpenZAF": 2.5, "FCAmo": 0.0},
    }
    base.update(over)
    return base


def test_has_live_values_rejects_session_closed_zero_rows():
    assert mid.has_live_values(_mi()) is True
    zero = {k: 0.0 for k in mid.FIELD_MAP.values()}
    zero["symbol"] = "600519"
    zero["raw"] = {"A": "0", "B": "0.00"}
    assert mid.has_live_values(zero) is False


def test_row_to_columns_maps_and_reads_open_pct_from_raw():
    cols = mid.row_to_columns(_mi())
    assert cols["total_mv"] == pytest.approx(21000.0)
    assert cols["chg_pct"] == pytest.approx(3.1)
    assert cols["consecutive_up_days"] == 2
    assert isinstance(cols["consecutive_up_days"], int)
    assert cols["open_pct"] == pytest.approx(2.5)     # 来自 raw.OpenZAF
    assert cols["raw"]["OpenZAF"] == pytest.approx(2.5)


def test_more_info_upsert_skips_zero_and_is_idempotent(maker):
    zero = {k: 0.0 for k in mid.FIELD_MAP.values()}
    zero.update({"symbol": "000001", "raw": {}})
    with maker() as s:
        assert mid.upsert_rows([_mi(), zero], trade_date="2026-10-16", session=s) == 1
        assert mid.upsert_rows(
            [_mi(change_pct=4.4)], trade_date="2026-10-16", session=s
        ) == 1
    with maker() as s:
        rows = s.scalars(select(MoreInfoDaily)).all()
    assert len(rows) == 1
    assert rows[0].chg_pct == pytest.approx(4.4)


def test_more_info_latest_and_history(maker):
    with maker() as s:
        for day, pct in (("2026-10-14", 1.0), ("2026-10-15", 2.0), ("2026-10-16", 3.0)):
            mid.upsert_rows([_mi(change_pct=pct)], trade_date=day, session=s)
    with maker() as s:
        latest = mid.latest("600519", session=s)
        hist = mid.history("600519", days=2, session=s)
    assert latest["trade_date"] == "2026-10-16"
    assert latest["chg_pct"] == pytest.approx(3.0)
    assert [h["trade_date"] for h in hist] == ["2026-10-15", "2026-10-16"]


def test_more_info_latest_returns_none_when_table_missing(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("no such table")

    class _Maker:
        def __call__(self):
            raise RuntimeError("no such table")

    assert mid.latest("600519", session_factory=_Maker()) is None


def test_more_info_capture_batches_and_skips_zero(maker):
    seen: list[list[str]] = []

    def fetcher(symbols, market):
        seen.append(list(symbols))
        return [_mi(symbol=s) if s != "000002" else
                {k: 0.0 for k in mid.FIELD_MAP.values()} | {"symbol": s, "raw": {}}
                for s in symbols]

    out = mid.capture_universe(
        ["600519", "000001", "000002"], batch=2, fetcher=fetcher,
        now=datetime(2026, 10, 16, 15, 10, 0), session_factory=maker,
    )
    assert out["ok"] is True
    assert out["captured"] == 2
    assert out["skipped_zero"] == 1
    assert [len(p) for p in seen] == [2, 1]
    with maker() as s:
        assert mid.latest("000002", session=s) is None


# ═══════════════════════════════════════════════════════════════════════════
# ⑥ 指数日K
# ═══════════════════════════════════════════════════════════════════════════
def _bars(prices: list[float]) -> list[dict]:
    return [
        {"date": f"2026-10-{i + 1:02d}", "open": p, "high": p + 1,
         "low": p - 1, "close": p, "volume": 1000 + i}
        for i, p in enumerate(prices)
    ]


def test_upsert_bars_chains_prev_close_and_is_idempotent(maker):
    with maker() as s:
        assert idxd.upsert_bars("000001", _bars([10.0, 10.5, 11.0]), session=s) == 3
    with maker() as s:
        rows = s.scalars(
            select(IndexDaily).where(IndexDaily.code == "000001")
            .order_by(IndexDaily.trade_date)
        ).all()
    assert [r.close for r in rows] == [10.0, 10.5, 11.0]
    assert rows[0].prev_close is None
    assert rows[1].prev_close == pytest.approx(10.0)
    assert rows[2].prev_close == pytest.approx(10.5)

    with maker() as s:
        idxd.upsert_bars("000001", _bars([10.0, 10.5, 12.0]), session=s)
        rows2 = s.scalars(
            select(IndexDaily).where(IndexDaily.code == "000001")
        ).all()
    assert len(rows2) == 3
    assert {r.close for r in rows2} == {10.0, 10.5, 12.0}


def test_load_recent_closes_ascending_and_limited(maker):
    with maker() as s:
        idxd.upsert_bars("399001", _bars([1.0, 2.0, 3.0, 4.0]), session=s)
        assert idxd.load_recent_closes("399001", days=3, session=s) == [2.0, 3.0, 4.0]
        assert idxd.load_recent_closes("999999", days=3, session=s) == []


def test_backfill_reports_honestly(maker):
    calls = {"n": 0}

    def fetcher(code, market, days):
        calls["n"] += 1
        if code == "HSI":
            return []
        return _bars([100.0, 101.0])

    out = idxd.backfill(fetcher=fetcher, session_factory=maker)
    assert out["ok"] is True
    assert out["rows"] == 2 * (len(idxd.INDEX_UNIVERSE) - 1)
    assert out["empty"] == 1 and out["failed"] == 0
    assert calls["n"] == len(idxd.INDEX_UNIVERSE)

    bad = idxd.backfill(fetcher=lambda *a, **k: [], session_factory=maker)
    assert bad["ok"] is False and "全部指数无可用日K" in bad["reason"]


def test_index_universe_matches_api_market_indices():
    from src.web.api.market import MARKET_INDICES

    assert {i["symbol"] for i in idxd.INDEX_UNIVERSE} == {
        i["symbol"] for i in MARKET_INDICES
    }
    assert {(i["symbol"], i["market"]) for i in idxd.INDEX_UNIVERSE} == {
        (i["symbol"], i["market"]) for i in MARKET_INDICES
    }


def test_spark_reads_db_first_and_falls_back(monkeypatch):
    from src.web.api import market

    market.clear_indices_cache()
    monkeypatch.setattr(
        "src.collectors.index_daily.load_recent_closes",
        lambda code, days=20, market=None: [9.7, 9.8, 9.9],
    )

    def _boom(*a, **kw):
        raise AssertionError("库里有 spark 时不应回源指数K线")

    monkeypatch.setattr(market, "get_index_klines", _boom)
    idx = {"symbol": "000001", "market": "CN", "tencent_symbol": "sh000001"}
    assert market._spark_for(idx) == [9.7, 9.8, 9.9]

    # 库里没有 → 回落取数(仍 fail-soft)
    market.clear_indices_cache()
    monkeypatch.setattr(
        "src.collectors.index_daily.load_recent_closes",
        lambda code, days=20, market=None: [],
    )
    monkeypatch.setattr(
        market, "get_index_klines",
        lambda code, mc, days=20: [
            type("K", (), {"close": 1.0})(), type("K", (), {"close": 2.0})()
        ],
    )
    assert market._spark_for(idx) == [1.0, 2.0]


def test_get_radar_rebuilds_when_intraday_db_row_is_stale(maker, monkeypatch):
    """盘中库行必须按 TTL 判新鲜: 10:00 的快照不能在 11:00 冒充实时。"""
    day = "2026-10-16"
    with maker() as s:
        cbs.upsert_daily_rows(
            s, [_row()], trade_date=day, universe=480,
            captured_at=datetime(2026, 10, 16, 10, 0, 0).timestamp(),
        )
    out = cb_radar.get_radar(
        limit=10,
        now=datetime(2026, 10, 16, 11, 0, 0),
        builder=lambda *, limit, now=None: _radar_payload(day),
        session_factory=maker,
    )
    assert out["cache"]["source"] == "live"
    assert out["cache"]["ttl_s"] == cb_radar.TTL_LIVE_S


def test_prime_terms_fills_cache_in_one_bulk_read(maker, clean_global_cb_terms):
    from src.db.session import SessionLocal

    today = __import__("datetime").date.today().isoformat()
    with SessionLocal() as s:
        cbs.upsert_terms(s, "113001", _TERM_RAW, as_of=today)
        cbs.upsert_terms(s, "123456", {**_TERM_RAW, "KZZName": "转债二号"}, as_of=today)

    cb_radar.clear_terms_cache()
    cb_radar._prime_terms(["113001", "123456", "999999"])
    cached = cb_radar._terms_cache
    assert cached["113001"][1]["KZZName"] == "转债一号"
    assert cached["123456"][1]["KZZName"] == "转债二号"
    assert "999999" not in cached


def test_pool_symbols_prefers_limit_up_pool_then_caps(monkeypatch):
    monkeypatch.setattr(
        "src.core.klines_fullmarket.limit_up_symbols",
        lambda: ["300750", "600519"],
    )
    monkeypatch.setattr(
        "src.collectors.stock_list.get_stock_list",
        lambda: [{"symbol": "000001"}, {"symbol": "600000"}, {"symbol": "300750"}],
    )
    assert ats.pool_symbols(3) == ["300750", "600519", "000001"]
    assert ats.pool_symbols(0) == ["300750", "600519", "000001", "600000"]


def test_more_info_endpoint_falls_back_to_daily_snapshot(monkeypatch):
    """盘后 TQ 会话值全 0 → 端点回退 more_info_daily(104 字段), 不再只能显"无数据"。"""
    import asyncio

    from src.web.api import quotes as quotes_api

    zero_live = {k: 0.0 for k in mid.FIELD_MAP.values()}
    monkeypatch.setattr(
        "src.core.marketdata_client.md_more_info",
        lambda syms, market="CN": [{**zero_live, "symbol": s} for s in syms],
    )
    monkeypatch.setattr(
        "src.core.more_info_daily.latest",
        lambda symbol, market="CN", **kw: {
            "symbol": symbol, "trade_date": "2026-10-16", "chg_pct": 3.1,
            "limit_up_ratio": 0.0, "raw": {"OpenZAF": "2.5"},
        },
    )
    out = asyncio.run(quotes_api.get_more_info("600519", "CN"))
    assert out["available"] is True
    assert out["source"] == "more_info_daily"
    assert out["as_of"] == "2026-10-16"
    assert out["chg_pct"] == pytest.approx(3.1)
    assert "收盘定稿" in out["note"]


def test_cb_snapshot_api_serves_db_without_any_vendor_call(monkeypatch, maker):
    """验收(方案 §8 批次1): 冷启动(进程缓存 + Redis 全空)首次请求读库出参。

    证明方式: 把 TQ 侧入口全部换成"被调用即失败", 仍能返回 available=True
    且 `cache.source='db'` —— 说明这条链一次 RPC 都没打。
    """
    from src.core.trade_phase import cn_now
    from src.web.api import cb_radar as cb_api
    from src.web.cache.biz_cache import biz_cache

    day = cn_now().date().isoformat()
    with maker() as s:
        cbs.upsert_daily_rows(s, [_row(), _row(code="123456")], trade_date=day, universe=488)

    monkeypatch.setattr("src.db.session.SessionLocal", maker)
    monkeypatch.setattr(
        cb_radar, "_universe",
        lambda: (_ for _ in ()).throw(AssertionError("读库命中时不应拉转债代码表")),
    )
    monkeypatch.setattr(
        "marketdata.vendors.tq.kzz_info",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("不应打 kzz_info")),
    )
    cb_radar.clear_terms_cache()
    biz_cache.delete("cb_radar:snapshot:10")

    out = cb_api.snapshot(limit=10, refresh=False)
    assert out["available"] is True
    assert out["cache"]["source"] == "db"
    assert out["universe"] == 488
    assert {r["code"] for r in out["rows"]} == {"113001", "123456"}
