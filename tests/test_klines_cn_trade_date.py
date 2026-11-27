"""CN 的交易日不受数据库连接时区影响。"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.db import klines_repo, session


def test_cn_daily_uses_shanghai_date_and_keeps_tq_preference(monkeypatch):
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = [
        # 北京 11/27 00:00（新时间戳）；同一个交易日仍有旧 UTC 午夜副本。
        (datetime(2026, 11, 26, 16, tzinfo=timezone.utc), 10, 11, 9, 10, 100, 'tencent'),
        (datetime(2026, 11, 27, 0, tzinfo=timezone.utc), 11, 12, 10, 11, 120, 'tq'),
    ]
    monkeypatch.setattr(session, 'engine', engine)
    bars = klines_repo.load_qfq_bars('600001', 'CN')
    assert [(bar['date'], bar['close']) for bar in bars] == [('2026-11-27', 11.0)]
