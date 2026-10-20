"""涨跌分布落库（2026-10-20）：首页不再每次重算。

原实现只有 biz_cache(300s)：TTL 一过 / 容器重启 / Redis 抖动就重拉新浪 79 页(~32s)，
前端只能显示「涨跌分布后台计算中」。落表后冷启动读库秒出，并留下盘中快照序列。
"""
from __future__ import annotations

from src.web.api import market_data as md


def test_age_and_stale_flags():
    fresh = md._breadth_with_age({"count": 10, "captured_at": "2000-01-01T00:00:00"})
    assert fresh["stale"] is True and fresh["age_s"] > 900

    none = md._breadth_with_age({"count": 10})
    assert none["age_s"] is None and none["stale"] is False


def test_save_then_read_roundtrip():
    payload = {
        "count": 5123,
        "total": 5300,
        "items": [{"bucket": "跌停", "count": 3}, {"bucket": "涨停", "count": 41}],
        "pending": False,
        "captured_at": None,
    }
    md._breadth_save(payload)
    got = md._breadth_from_db()
    assert got is not None
    assert got["count"] == 5123 and got["total"] == 5300
    assert got["items"][1] == {"bucket": "涨停", "count": 41}
    assert got["pending"] is False


def test_pending_or_empty_payload_is_not_persisted():
    before = md._breadth_from_db()
    md._breadth_save({"count": 0, "total": 0, "items": [], "pending": True})
    md._breadth_save({"count": 0, "total": 0, "items": [], "pending": False})
    after = md._breadth_from_db()
    assert (before or {}).get("count") == (after or {}).get("count")


def test_read_ignores_stale_rows():
    """超过 max_age_s 的快照不再当"当前"返回（宁可不给，也不拿隔夜数据冒充）。"""
    from datetime import datetime, timedelta

    from src.db.models import BreadthDistributionSnapshot
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        db.query(BreadthDistributionSnapshot).delete()
        db.add(BreadthDistributionSnapshot(
            trade_date="2026-01-01", captured_at=datetime.now() - timedelta(hours=2),
            total=50, valid=42, items='[{"bucket": "涨停", "count": 1}]', source="test",
        ))
        db.commit()
    finally:
        db.close()

    assert md._breadth_from_db(max_age_s=7200) is not None      # 2h 内算新鲜
    assert md._breadth_from_db(max_age_s=60) is None            # 超过 60s 就不给


def test_migration_206_registered():
    from src.web.migrations import MIGRATIONS

    versions = {m.version for m in MIGRATIONS}
    assert 206 in versions
    assert max(versions) == 206          # 新增迁移必须接在末尾，不能插队
