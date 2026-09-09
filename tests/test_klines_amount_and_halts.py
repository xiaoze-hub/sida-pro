"""B1.3/B1.5: klines amount 列 + 停牌区间表。"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册 ORM 模型
from src.web.database import Base

_KLINES_DDL = (
    "CREATE TABLE klines ("
    "ts DATETIME, symbol TEXT, market TEXT, period TEXT, source TEXT, adjust TEXT, "
    "open REAL, high REAL, low REAL, close REAL, volume INTEGER, quality_flag INTEGER)"
)


def _mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)()


# ──────────────── B1.3 amount 列 ────────────────

def test_to_db_row_carries_amount_when_available():
    from src.collectors.klines_ingestor import _to_db_row

    k = SimpleNamespace(date="2026-09-01", open=10.0, high=10.5, low=9.8,
                        close=10.2, volume=1000, amount=10200.0)
    row = _to_db_row("600000", "CN", "1d", "tencent", k, ts="2026-09-01 00:00:00")
    assert row["amount"] == 10200.0

    k2 = SimpleNamespace(date="2026-09-01", open=10.0, high=10.5, low=9.8,
                         close=10.2, volume=1000)
    row2 = _to_db_row("600000", "CN", "1d", "tencent", k2, ts="2026-09-01 00:00:00")
    assert row2["amount"] is None  # 缺失留 NULL, 不伪造


def test_migration_150_adds_amount_column_idempotently():
    from src.web.migrations import _m150_klines_amount_column

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(_KLINES_DDL))
        _m150_klines_amount_column(conn)
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(klines)")).fetchall()]
        assert "amount" in cols
        _m150_klines_amount_column(conn)  # 幂等重跑
        cols2 = [r[1] for r in conn.execute(text("PRAGMA table_info(klines)")).fetchall()]
        assert cols2.count("amount") == 1


# ──────────────── B1.5 停牌区间 ────────────────

def test_halt_lifecycle_and_boundaries():
    from src.core.halts import is_halted, load_halts, upsert_halt

    _engine, db = _mem_db()
    try:
        assert upsert_halt(db, symbol="600000", start_date="2026-09-02",
                           end_date="2026-09-04", reason="重大事项") == 1
        db.commit()

        assert is_halted("600000", "2026-09-01", db=db) is False   # 停牌前
        assert is_halted("600000", "2026-09-02", db=db) is True    # 起始日
        assert is_halted("600000", "2026-09-03", db=db) is True    # 区间内
        assert is_halted("600000", "2026-09-04", db=db) is True    # 结束日
        assert is_halted("600000", "2026-09-05", db=db) is False   # 复牌后
        assert is_halted("600001", "2026-09-03", db=db) is False   # 其它标的

        # 开放区间(end 为空) → 查询日之后仍算停牌
        upsert_halt(db, symbol="600002", start_date="2026-09-02")
        db.commit()
        assert is_halted("600002", "2026-09-30", db=db) is True

        # 幂等: 同 (symbol, start) 重写不新增行
        upsert_halt(db, symbol="600000", start_date="2026-09-02",
                    end_date="2026-09-06", reason="延长")
        db.commit()
        rows = load_halts("600000", db=db)
        assert len(rows) == 1 and rows[0]["end_date"] == "2026-09-06"
    finally:
        db.close()


def test_migration_151_creates_trading_halts():
    from src.web.migrations import _m151_trading_halts_table

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        _m151_trading_halts_table(conn)
        _m151_trading_halts_table(conn)  # 幂等
        cols = [
            r[1]
            for r in conn.execute(text("PRAGMA table_info(trading_halts)")).fetchall()
        ]
        assert {"symbol", "market", "start_date", "end_date", "reason"} <= set(cols)
