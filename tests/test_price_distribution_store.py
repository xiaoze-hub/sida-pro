# -*- coding: utf-8 -*-
"""I-8 分价矩阵落库/读穿透测试(2026-10-18)。

守用户硬约束「所有数据必须全部落库」:
  ① 盘中快照按 (交易日, 票, 快照时刻) **原地覆盖**, 不因 15s 轮询把表撑爆;
  ② 日终归档按 (交易日, 票) 唯一, 同日重复归档覆盖;
  ③ 读穿透: 新鲜→读库(不打上游) / 过期→回源并落库 / 上游挂→旧行 + stale 标注;
  ④ 降级结果**不落库**(否则回测集里会留下"当天无分价"的假事实)。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import src.db.models  # noqa: F401 - 注册 ORM 模型
from src.core.price_distribution_store import (
    archive_daily,
    get_or_refresh,
    load_daily_history,
    load_latest_snapshot,
    purge_old_snapshots,
    upsert_snapshot,
)
from src.db.models import PriceDistributionDaily, PriceDistributionSnapshot
from src.db.session import Base


def _analysis(**over):
    base = {
        "available": True,
        "symbol": "002361",
        "market": "CN",
        "trade_date": "2026-10-16",
        "snapshot_ts": "15:00:02",
        "quote": {"close": 9.06},
        "day": {
            "total_vol": 67000.0,
            "buy_vol": 39699.0,
            "sell_vol": 27301.0,
            "net_amount": 12398.0 * 9.0 * 100,
            "buy_ratio": 0.5925,
            "big_ratio": 0.006,
        },
        "bins": [{"price": 9.05, "volume": 30000}],
        "support_levels": [{"price": 9.05, "buy_ratio": 0.78}],
        "resistance_levels": [{"price": 9.30, "buy_ratio": 0.2}],
        "iceberg_signals": [],
        "quality": {"bins": 1},
        "source": "tencent_price",
    }
    base.update(over)
    return base


@pytest.fixture()
def session_factory(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/pd.db")
    Base.metadata.create_all(bind=eng)
    maker = sessionmaker(bind=eng)
    yield maker
    eng.dispose()


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
def test_upsert_snapshot_is_idempotent_per_snapshot_time(session_factory):
    with session_factory() as s:
        assert upsert_snapshot(s, _analysis(), captured_at=1000.0) is True
        assert upsert_snapshot(
            s, _analysis(day={**_analysis()["day"], "total_vol": 68000.0}), captured_at=1010.0
        ) is True
        rows = s.scalars(select(PriceDistributionSnapshot)).all()
        assert len(rows) == 1                      # 同一快照时刻 → 覆盖, 不新增
        assert rows[0].total_vol == pytest.approx(68000.0)
        assert rows[0].captured_at == pytest.approx(1010.0)
        assert rows[0].price_bins_json


def test_upsert_snapshot_skips_unavailable_results(session_factory):
    with session_factory() as s:
        assert upsert_snapshot(s, _analysis(available=False, reason="源挂了")) is False
        assert s.scalar(select(func.count()).select_from(PriceDistributionSnapshot)) == 0


def test_archive_daily_keeps_one_row_per_day_and_updates(session_factory):
    with session_factory() as s:
        assert archive_daily(s, _analysis(), settled_at=2000.0) is True
        assert archive_daily(
            s,
            _analysis(snapshot_ts="14:55:00", support_levels=[{"price": 9.11, "buy_ratio": 0.7}]),
            settled_at=2100.0,
        ) is True
        rows = s.scalars(select(PriceDistributionDaily)).all()
        assert len(rows) == 1
        assert rows[0].settled_at == pytest.approx(2100.0)
        assert rows[0].snapshot_ts == "14:55:00"
        assert rows[0].support_price == pytest.approx(9.11)
        assert rows[0].resistance_price == pytest.approx(9.30)


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def test_load_latest_snapshot_honours_max_age(session_factory):
    with session_factory() as s:
        upsert_snapshot(s, _analysis(), captured_at=1000.0)
    with session_factory() as s:
        assert load_latest_snapshot(s, "002361", now_ts=1030.0, max_age_s=60) is not None
        assert load_latest_snapshot(s, "002361", now_ts=1200.0, max_age_s=60) is None


def test_load_daily_history_is_ascending_and_limited(session_factory):
    with session_factory() as s:
        for d in ("2026-10-14", "2026-10-15", "2026-10-16"):
            archive_daily(s, _analysis(trade_date=d), settled_at=1.0)
    with session_factory() as s:
        items = load_daily_history(s, "002361", days=2)
        assert [i["trade_date"] for i in items] == ["2026-10-15", "2026-10-16"]
        assert items[-1]["payload"]["available"] is True


def test_purge_old_snapshots_keeps_daily_archive(session_factory):
    with session_factory() as s:
        upsert_snapshot(s, _analysis(trade_date="2026-10-01"), captured_at=1.0)
        upsert_snapshot(s, _analysis(trade_date="2026-10-16"), captured_at=2.0)
        archive_daily(s, _analysis(trade_date="2026-10-01"), settled_at=1.0)
    with session_factory() as s:
        removed = purge_old_snapshots(s, keep_days=5, today="2026-10-16")
        assert removed == 1
        left = s.scalars(select(PriceDistributionSnapshot)).all()
        assert [r.trade_date for r in left] == ["2026-10-16"]
        assert s.scalar(select(func.count()).select_from(PriceDistributionDaily)) == 1


# --------------------------------------------------------------------------- #
# 读穿透
# --------------------------------------------------------------------------- #
def test_get_or_refresh_serves_fresh_db_row_without_calling_upstream(session_factory):
    with session_factory() as s:
        upsert_snapshot(s, _analysis(), captured_at=1000.0)

    called = {"n": 0}

    def fetcher(*a, **kw):
        called["n"] += 1
        return _analysis()

    out = get_or_refresh(
        "002361", "CN", ttl_s=60, now_ts=1010.0,
        fetcher=fetcher, session_factory=session_factory,
    )
    assert out["cache"]["hit"] is True
    assert out["cache"]["source"] == "db"
    assert out["available"] is True
    assert called["n"] == 0                     # 秒开的关键: 不打上游


def test_get_or_refresh_fetches_and_persists_when_stale(session_factory):
    with session_factory() as s:
        upsert_snapshot(s, _analysis(trade_date="2026-10-15"), captured_at=1000.0)

    def fetcher(*a, **kw):
        return _analysis(trade_date="2026-10-16")

    out = get_or_refresh(
        "002361", "CN", ttl_s=60, now_ts=1100.0,
        fetcher=fetcher, session_factory=session_factory,
    )
    assert out["cache"]["source"] == "live"
    assert out["cache"]["persist_error"] is None
    with session_factory() as s:
        dates = sorted(r.trade_date for r in s.scalars(select(PriceDistributionSnapshot)).all())
    assert dates == ["2026-10-15", "2026-10-16"]  # 回源结果已落库


def test_get_or_refresh_refresh_flag_bypasses_cache(session_factory):
    with session_factory() as s:
        upsert_snapshot(s, _analysis(), captured_at=1000.0)
    called = {"n": 0}

    def fetcher(*a, **kw):
        called["n"] += 1
        return _analysis()

    out = get_or_refresh(
        "002361", "CN", ttl_s=60, now_ts=1001.0, refresh=True,
        fetcher=fetcher, session_factory=session_factory,
    )
    assert called["n"] == 1
    assert out["cache"]["source"] == "live"


def test_get_or_refresh_falls_back_to_stale_row_when_upstream_dies(session_factory):
    with session_factory() as s:
        upsert_snapshot(s, _analysis(), captured_at=1000.0)

    def fetcher(*a, **kw):
        raise RuntimeError("腾讯超时")

    out = get_or_refresh(
        "002361", "CN", ttl_s=60, now_ts=5000.0,
        fetcher=fetcher, session_factory=session_factory,
    )
    assert out["available"] is True
    assert out["cache"]["stale"] is True
    assert out["cache"]["source"] == "db_stale"
    assert "腾讯超时" in (out["cache"]["reason"] or "")


def test_get_or_refresh_returns_degraded_when_nothing_cached(session_factory):
    out = get_or_refresh(
        "002361", "CN", fetcher=lambda *a, **kw: {"available": False, "reason": "无数据"},
        session_factory=session_factory, now_ts=1000.0,
    )
    assert out["available"] is False
    assert out["cache"]["source"] == "none"
    assert out["cache"]["reason"] == "无数据"


def test_get_or_refresh_survives_db_write_failure(session_factory):
    """落库失败不能把一次成功的取数变成 500: 返回现算结果 + persist_error。"""
    calls = {"n": 0}

    def factory():
        calls["n"] += 1
        if calls["n"] == 1:          # 第一次(读)正常
            return session_factory()
        raise RuntimeError("db is locked")

    out = get_or_refresh(
        "002361", "CN", fetcher=lambda *a, **kw: _analysis(),
        session_factory=factory, now_ts=1000.0,
    )
    assert out["available"] is True
    assert out["cache"]["persist_error"]
    assert "db is locked" in out["cache"]["persist_error"]


# --------------------------------------------------------------------------- #
# 收盘批量归档(cron 入口)
# --------------------------------------------------------------------------- #
def test_archive_symbols_batches_all_tickers(session_factory):
    from src.core.price_distribution_archive import archive_symbols

    def fetcher(sym, market, **kw):
        if sym == "000002":
            return {"available": False, "reason": "停牌无分价", "symbol": sym}
        return _analysis(symbol=sym)

    out = archive_symbols(
        [("002361", "CN"), ("000002", "CN"), ("600519", "CN")],
        fetcher=fetcher, session_factory=session_factory, sleep_s=0,
    )
    assert out["total"] == 3
    assert out["archived"] == 2
    assert out["skipped"] == 1
    assert any("000002" in e for e in out["errors"])

    with session_factory() as s:
        rows = s.scalars(select(PriceDistributionDaily)).all()
    assert sorted(r.symbol for r in rows) == ["002361", "600519"]


def test_archive_symbols_respects_limit(session_factory):
    from src.core.price_distribution_archive import archive_symbols

    out = archive_symbols(
        [("002361", "CN"), ("600519", "CN")], limit=1,
        fetcher=lambda sym, market, **kw: _analysis(symbol=sym),
        session_factory=session_factory, sleep_s=0,
    )
    assert out["total"] == 1


def test_register_archive_cron_requires_scheduler():
    from src.core.price_distribution_archive import register_archive_cron

    assert register_archive_cron(None) is False

    class _Sched:
        def __init__(self):
            self.jobs = []

        def add_job(self, fn, trigger, **kw):
            self.jobs.append((fn, trigger, kw))

    sched = _Sched()
    assert register_archive_cron(sched) is True
    assert sched.jobs[0][2]["id"] == "price_distribution_archive"


def test_snapshot_buckets_to_five_minutes(session_factory):
    """盘中 3~5s 刷新一次, 按秒落库会把证据存成录像 → 5 分钟桶内只留最后一次。"""
    from src.core.price_distribution_store import bucket_of

    assert bucket_of("14:57:03") == "14:55"
    assert bucket_of("10:03:59") == "10:00"
    assert bucket_of("09:59:59") == "09:55"
    assert bucket_of("") == ""

    with session_factory() as s:
        upsert_snapshot(s, _analysis(snapshot_ts="10:01:03"), captured_at=1.0)
        upsert_snapshot(s, _analysis(snapshot_ts="10:04:58"), captured_at=2.0)
        rows = s.scalars(select(PriceDistributionSnapshot)).all()
        assert len(rows) == 1                      # 同一 5 分钟桶 → 覆盖
        assert rows[0].snapshot_ts == "10:04:58"   # 桶内保留最新
        assert rows[0].bucket_ts == "10:00"

        upsert_snapshot(s, _analysis(snapshot_ts="10:05:01"), captured_at=3.0)
        rows = s.scalars(select(PriceDistributionSnapshot).order_by(
            PriceDistributionSnapshot.bucket_ts)).all()
        assert [r.bucket_ts for r in rows] == ["10:00", "10:05"]
