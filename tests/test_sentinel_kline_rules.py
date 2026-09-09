"""B1.2(KI-040): 哨兵 K 线质量规则 —— OHLC 关系 / 缺口超限 / quality_flag。"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册 ORM 模型
from src.web.database import Base

_DDL = (
    "CREATE TABLE klines ("
    "ts DATETIME, symbol TEXT, market TEXT, period TEXT, source TEXT, adjust TEXT, "
    "open REAL, high REAL, low REAL, close REAL, volume INTEGER, quality_flag INTEGER)"
)


def _mem_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.execute(text(_DDL))
    db.commit()
    return db


def _insert(db, symbol, ts, o, h, l, c, flag=1):
    db.execute(
        text(
            "INSERT INTO klines (ts, symbol, market, period, source, adjust, "
            "open, high, low, close, volume, quality_flag) VALUES "
            "(:ts, :s, 'CN', '1d', 'tencent', 'qfq', :o, :h, :l, :c, 1000, :f)"
        ),
        {"ts": ts, "s": symbol, "o": o, "h": h, "l": l, "c": c, "f": flag},
    )


def test_kline_quality_flags_ohlc_and_gap():
    from src.core.data_quality_sentinel import _check_kline_quality

    db = _mem_db()
    try:
        now = datetime(2026, 9, 10, 16, 0)
        # 正常柱 + 缺口超限(600000: 10 → 13 = +30%) + OHLC 关系异常
        _insert(db, "600000", now - timedelta(days=3), 10, 10.5, 9.8, 10.0)
        _insert(db, "600000", now - timedelta(days=2), 12.0, 13.1, 11.9, 13.0)
        _insert(db, "600001", now - timedelta(days=2), 10.0, 9.5, 9.0, 9.8)
        db.commit()

        res = _check_kline_quality(db, now)
        assert res["check"] == "kline_quality"
        assert res["status"] == "warn"
        assert res["value"] == 2  # 1 跳变 + 1 OHLC 异常
        assert "OHLC异常=1" in res["detail"] and "跳变=1" in res["detail"]
    finally:
        db.close()


def test_kline_quality_ok_when_clean():
    from src.core.data_quality_sentinel import _check_kline_quality

    db = _mem_db()
    try:
        now = datetime(2026, 9, 10, 16, 0)
        for i in range(5):
            _insert(db, "600000", now - timedelta(days=5 - i), 10 + i * 0.05,
                    10.2 + i * 0.05, 9.9 + i * 0.05, 10.1 + i * 0.05)
        db.commit()
        res = _check_kline_quality(db, now)
        assert res["status"] == "ok" and res["value"] == 0
    finally:
        db.close()


def test_kline_quality_handles_missing_table():
    """表不存在/查询失败 → ok(不阻断其它检查)。"""
    from src.core.data_quality_sentinel import _check_kline_quality

    db = _mem_db()
    try:
        db.execute(text("DROP TABLE klines"))
        db.commit()
        res = _check_kline_quality(db, datetime(2026, 9, 10, 16, 0))
        assert res["status"] == "ok" and "跳过" in res["detail"]
    finally:
        db.close()
