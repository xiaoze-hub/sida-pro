"""逐笔日存档单测 (#3 回测底座 + #4 跨日序列, 2026-09-04)。

sqlite :memory: 跑全链路, 不联网不碰生产库。
"""
import datetime
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packages/marketdata/src"))

import pytest
from sqlalchemy import create_engine

from src.core import tick_archive as ta


@pytest.fixture()
def mem_engine():
    from sqlalchemy import text
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE tick_archive (trade_date TEXT NOT NULL, code TEXT NOT NULL, "
            "symbol TEXT NOT NULL, tick_count INTEGER, last_tick_t TEXT, tick_pages INTEGER, "
            "main_net DOUBLE PRECISION, big_net DOUBLE PRECISION, mid_net DOUBLE PRECISION, "
            "retail_net DOUBLE PRECISION, data_status TEXT, signal TEXT, ticks_json TEXT, "
            "created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP, UNIQUE (code, trade_date))"
        ))
    return eng


def _result(net=100.0):
    return {"tick_count": 40, "last_tick_t": "15:00:00", "tick_pages": 2,
            "main_net": net, "big_net": net, "mid_net": 0.0, "small_net": 1.0,
            "data_status": "ok", "signal": "s"}


def _ticks(n=40):
    return [{"d": "B", "amt": 1.0, "t": "09:25:00"} for _ in range(n)]


def test_past_close_gate():
    assert ta._past_close(datetime.datetime(2026, 9, 4, 15, 6, 0)) is True
    assert ta._past_close(datetime.datetime(2026, 9, 4, 14, 59, 0)) is False


def test_upsert_and_series(mem_engine):
    with mem_engine.begin() as conn:
        ta.upsert_day(conn, "sz002361", "002361", "2026-09-03", _ticks(), _result(100.0))
        ta.upsert_day(conn, "sz002361", "002361", "2026-09-04", _ticks(), _result(-50.0))
        # 幂等: 同日重写覆盖
        ta.upsert_day(conn, "sz002361", "002361", "2026-09-04", _ticks(), _result(-60.0))
    rows = ta.read_series("002361", engine=mem_engine)
    assert [r["trade_date"] for r in rows] == ["2026-09-04", "2026-09-03"]
    assert rows[0]["main_net"] == -60.0
    assert rows[0]["tick_count"] == 40


def test_maybe_archive_gates_no_db():
    """白天/空数据直接 False, 不碰 DB(传坏 engine 也应 False)。"""
    assert ta.maybe_archive_day("sz002361", "002361", [], _result()) is False
