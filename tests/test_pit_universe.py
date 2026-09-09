"""B0.6: PIT 股票池 —— 按 as_of 取池(消除幸存者偏差), ST 未知保守 5%。"""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册 ORM 模型
from src.web.database import Base


def _mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_universe_as_of_filters_delisted_and_st():
    from src.core.universe import universe_as_of, upsert_universe

    db = _mem_db()
    try:
        n = upsert_universe(
            db,
            as_of_date="2026-08-01",
            rows=[
                {"symbol": "600000", "market": "CN", "stock_name": "浦发银行"},
                {"symbol": "600001", "market": "CN", "stock_name": "退市A", "is_delisted": True},
                {"symbol": "600002", "market": "CN", "stock_name": "*ST某某"},
            ],
            source="test",
        )
        db.commit()
        assert n == 3

        got = universe_as_of("2026-08-01", db=db)
        assert set(got) == {"600000", "600002"}, "退市标的应剔除"
        assert universe_as_of("2026-08-01", include_st=False, db=db) == ["600000"]
        assert universe_as_of("2026-07-31", db=db) == [], "无快照必须返回空, 不静默用今天名单"
    finally:
        db.close()


def test_upsert_universe_is_idempotent():
    from src.core.universe import universe_as_of, upsert_universe

    db = _mem_db()
    try:
        rows = [{"symbol": "600000", "market": "CN", "stock_name": "浦发银行"}]
        upsert_universe(db, as_of_date="2026-08-01", rows=rows, source="test")
        upsert_universe(db, as_of_date="2026-08-01", rows=rows, source="test")
        db.commit()
        assert universe_as_of("2026-08-01", db=db) == ["600000"], "重复写入不得产生重复行"
    finally:
        db.close()


def test_filter_symbols_falls_back_when_no_snapshot():
    from src.core.universe import filter_symbols, upsert_universe

    db = _mem_db()
    try:
        upsert_universe(
            db,
            as_of_date="2026-08-01",
            rows=[{"symbol": "600000", "market": "CN"}],
            source="test",
        )
        db.commit()
        assert filter_symbols(["600000", "999999"], "2026-08-01", db=db) == ["600000"]
        # 无快照 → 原样返回(但会告警)
        assert filter_symbols(["600000", "999999"], "2026-07-01", db=db) == ["600000", "999999"]
    finally:
        db.close()


def test_limit_ratio_unknown_st_is_conservative():
    """B0.6: ST 未知 → 保守 5%; 显式 False 才按主板 10%。"""
    from src.core.limit_rules import limit_ratio, limit_up_price

    assert limit_ratio("600000") == 0.05
    assert limit_ratio("600000", False) == 0.10
    assert limit_ratio("600000", True) == 0.05
    assert limit_up_price("600000", 10.0, False) == 11.0
    assert limit_up_price("600000", 10.0) == 10.5
