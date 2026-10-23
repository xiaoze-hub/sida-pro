"""exday 全市场采集器：落库幂等 + 缺表降级 + 非交易日不写半场数据。

不打真网关（TQ 只在生产小主机可达）：fetch 一律 monkeypatch 成桩，
本文件只钉"数据怎么进库/怎么降级"这两件事。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.collectors import exday_daily
from src.db.models import Base, ExdayDaily


@pytest.fixture()
def db():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng, tables=[ExdayDaily.__table__])
    Session = sessionmaker(bind=eng)
    s = Session()
    try:
        yield s
    finally:
        s.close()


def _row(day="20261023", symbol="002361", **kw):
    base = {
        "trade_date": day, "symbol": symbol, "market": "CN",
        "amount": 5.48e8, "volume_lots": 640592.0,
        "active_buy_amt": 2.76e8, "active_sell_amt": 2.72e8, "active_ratio": 50.4,
        "retail_ratio": 56.9, "xl_share": 2.4, "main_active_net": 5.28e6,
        "main_active_net_pct": 0.96, "sm_active_net": 5.06e6, "chip_migration": 2.2e5,
        "gran_hiding": 4.35, "order_fill_ratio": 0.997,
        "b_order": 1005818.75, "s_order": 990056.75,
        "total_b_order": 27903.0, "total_s_order": 49328.0,
        "b_cancel": 337298.0, "s_cancel": 300111.0, "cjbs": 77398.0,
        "quality": "ok", "reason": None, "source": "tdx_tq_exday",
    }
    base.update(kw)
    return base


def test_upsert_is_idempotent(db):
    assert exday_daily.upsert_rows(db, [_row()]) == 1
    assert db.query(ExdayDaily).count() == 1
    # 同一天同一票再写一次 → 更新而非新增
    assert exday_daily.upsert_rows(db, [_row(amount=6.0e8)]) == 1
    assert db.query(ExdayDaily).count() == 1
    assert db.query(ExdayDaily).one().amount == 6.0e8


def test_upsert_batches_multiple_symbols(db):
    rows = [_row(symbol=f"00{i:04d}") for i in range(1, 6)]
    assert exday_daily.upsert_rows(db, rows) == 5
    assert db.query(ExdayDaily).count() == 5


def test_missing_table_degrades_instead_of_raising(db):
    """迁移没跑（表不存在）时整批跳过并返回 0，不炸采集作业。"""
    db.execute(__import__("sqlalchemy").text("DROP TABLE exday_daily"))
    db.commit()
    assert exday_daily.upsert_rows(db, [_row()]) == 0


def test_collect_once_counts_failures_without_aborting(db, monkeypatch):
    def fake_fetch(symbol):
        if symbol == "bad":
            return None
        return _row(symbol=symbol)

    monkeypatch.setattr(exday_daily, "fetch_one", fake_fetch)
    res = exday_daily.collect_once(db, symbols=["a", "bad", "b"], trade_date="20261023")
    assert res["written"] == 2
    assert res["failed"] == 1
    assert res["ok"] is True

    # 全部失败 → ok=False（作业框架判 failed，不假装成功）
    res2 = exday_daily.collect_once(db, symbols=["bad", "bad"], trade_date="20261022")
    assert res2["ok"] is False and res2["written"] == 0


def test_collect_once_uses_batch_date(db, monkeypatch):
    """跨日跑批时统一写批次日，防止一条数据落到两个日期。"""
    monkeypatch.setattr(
        exday_daily, "fetch_one",
        lambda s: _row(symbol=s, trade_date="20261024"),
    )
    exday_daily.collect_once(db, symbols=["002361"], trade_date="2026-10-23")
    got = db.query(ExdayDaily).one()
    assert got.trade_date == "20261023"


def test_empty_pool_is_explicit_failure(db, monkeypatch):
    monkeypatch.setattr(exday_daily, "targets", lambda db, limit=None: [])
    res = exday_daily.collect_once(db)
    assert res["ok"] is False
    assert "股票池为空" in res["reason"]


def test_write_through_swallow_errors(db, monkeypatch):
    """写穿失败不能影响工具返回（工具层还有实时结果可用）。"""
    import src.core.exday_structure as ex

    def boom(*a, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(ex, "fetch", boom)
    assert exday_daily.write_through(db, "002361") is None


def test_market_day_guard_uses_calendar(monkeypatch):
    """交易日判定必须走 trading_calendar（节假日不能写空数据）。"""
    import src.core.trading_calendar as cal

    monkeypatch.setattr(cal, "is_trading_day", lambda d: False)
    res = exday_daily.run_scheduled()
    assert res.get("skipped") is True
