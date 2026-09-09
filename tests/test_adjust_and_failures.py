"""B1.4/B1.6: 除权因子折算 + 数据源失败明细(限流)。"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册 ORM 模型
from src.web.database import Base


def _mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


# ──────────────── B1.4 除权因子 ────────────────

def test_adjust_close_applies_future_ex_dates_only():
    from src.core.adjust import adjust_close, load_adj_factors, upsert_adj_factor

    _engine, db = _mem_db()
    try:
        upsert_adj_factor(db, symbol="600000", ex_date="2026-06-10", ratio=0.5)
        db.commit()
        factors = load_adj_factors("600000", db=db)
        assert len(factors) == 1

        # 除权日之前的价格要乘因子(前复权下调)
        assert adjust_close(20.0, "2026-06-09", factors) == 10.0
        # 除权日当天及之后不折算
        assert adjust_close(10.0, "2026-06-10", factors) == 10.0
        assert adjust_close(10.0, "2026-06-11", factors) == 10.0
    finally:
        db.close()


def test_to_qfq_series_scales_ohlc():
    from src.core.adjust import to_qfq_series

    factors = [{"ex_date": "2026-06-10", "ratio": 0.5}]
    raw = [
        {"date": "2026-06-09", "open": 20.0, "high": 21.0, "low": 19.0, "close": 20.5},
        {"date": "2026-06-10", "open": 10.0, "high": 10.4, "low": 9.9, "close": 10.2},
    ]
    out = to_qfq_series(raw, factors)
    assert out[0]["close"] == 10.25 and out[0]["adj_factor"] == 0.5
    assert out[1]["close"] == 10.2 and out[1]["adj_factor"] == 1.0


def test_upsert_adj_factor_idempotent():
    from src.core.adjust import load_adj_factors, upsert_adj_factor

    _engine, db = _mem_db()
    try:
        upsert_adj_factor(db, symbol="600000", ex_date="2026-06-10", ratio=0.5)
        upsert_adj_factor(db, symbol="600000", ex_date="2026-06-10", ratio=0.4)
        db.commit()
        rows = load_adj_factors("600000", db=db)
        assert len(rows) == 1 and rows[0]["ratio"] == 0.4
    finally:
        db.close()


# ──────────────── B1.6 失败明细 ────────────────

def test_datasource_failure_persist_and_dedupe():
    from src.core import datasource_failures as DF

    _engine, db = _mem_db()
    try:
        DF.reset_dedupe()
        assert DF.record("tencent", "timeout", symbol="600000", db=db) is True
        assert DF.record("tencent", "timeout", symbol="600001", db=db) is False, "60s 内同源同类型只落一条"
        assert DF.record("tencent", "parse", db=db) is True, "不同 kind 独立限流"

        rows = DF.recent(limit=10, db=db)
        assert len(rows) == 2
        assert {r["provider"] for r in rows} == {"tencent"}
        assert {r["kind"] for r in rows} == {"timeout", "parse"}
    finally:
        db.close()


def test_record_datasource_failure_writes_detail(monkeypatch):
    """health.record_datasource_failure 也应落明细(经同一限流)。"""
    from src.core import datasource_failures as DF
    from src.web.api.health import record_datasource_failure

    engine, _db = _mem_db()
    monkeypatch.setattr(DF, "SessionLocal", sessionmaker(bind=engine))
    DF.reset_dedupe()
    record_datasource_failure("eastmoney", kind="auth")
    rows = DF.recent(limit=5)
    assert any(r["provider"] == "eastmoney" and r["kind"] == "auth" for r in rows)


def test_migrations_152_153_create_tables():
    from src.web.migrations import (
        _m152_adj_factors_table,
        _m153_datasource_failures_table,
    )

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _m152_adj_factors_table(conn)
        _m153_datasource_failures_table(conn)
        from sqlalchemy import text

        names = {
            r[0]
            for r in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        assert {"adj_factors", "datasource_failures"} <= names
