# -*- coding: utf-8 -*-
"""l2_ticks 事件时间化(方案A, v0.5.49): 写入方 + 迁移 v158/v159 单测。

覆盖:
  - _l2_event_ts: 盘中/恰等/隔日拉取归前一交易日/非法 tick_time 回退/幂等
  - persist_l2_ticks: 事件 ts 落库 + 批次窗口计数精确 + 重放幂等 +
    跨日同 tick_time 不再互相顶掉(旧键缺陷修复) + PG 跳过 in-code retention
  - v158: 新库建新形态 / 空表重塑 / 有数据 no-op(O(1) 探测, 防 8s 超时)
  - v159: sqlite no-op(PG 专属)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import src.core.history_store as hs  # noqa: E402
import src.db.session as dbs  # noqa: E402
from src.web.migrations import (  # noqa: E402
    _m128_l2_ticks_table,
    _m158_l2_ticks_event_time_key,
    _m159_klines_minute_continuous_agg,
)

CN = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _mk_engine(migrator):
    eng = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with eng.begin() as conn:
        migrator(conn)
    return eng


def _patch_now(monkeypatch, dt: datetime) -> None:
    class _FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return dt

    monkeypatch.setattr(hs, "datetime", _FixedDT)


def _tick(d="b", t="09:30:05", price=10.0, vol=100.0, amt=1000.0):
    return {"d": d, "price": price, "vol": vol, "amt": amt, "t": t}


# ---------------------------------------------------------------------------
# _l2_event_ts 纯函数
# ---------------------------------------------------------------------------
def test_event_ts_intraday_same_day():
    now = datetime(2026, 9, 10, 8, 40, 15, tzinfo=timezone.utc)  # CN 16:40
    assert hs._l2_event_ts("09:30:05", now) == datetime(2026, 9, 10, 9, 30, 5, tzinfo=CN)


def test_event_ts_equal_tick_time_stays_same_day():
    now = datetime(2026, 9, 10, 1, 30, 5, tzinfo=timezone.utc)  # CN 09:30:05
    assert hs._l2_event_ts("09:30:05", now) == datetime(2026, 9, 10, 9, 30, 5, tzinfo=CN)


def test_event_ts_after_midnight_maps_prev_day():
    now = datetime(2026, 9, 11, 16, 5, 0, tzinfo=timezone.utc)  # CN 00:05 09-12
    assert hs._l2_event_ts("15:00:00", now) == datetime(2026, 9, 11, 15, 0, 0, tzinfo=CN)


def test_event_ts_malformed_falls_back_to_now():
    now = datetime(2026, 9, 10, 8, 40, 15, tzinfo=timezone.utc)
    for bad in (None, "", "9:30:00", "24:00:00", "093000", "09:30:0a"):
        assert hs._l2_event_ts(bad, now) == now


def test_event_ts_idempotent_on_event_rows():
    now = datetime(2026, 9, 10, 8, 40, 15, tzinfo=timezone.utc)
    ev = hs._l2_event_ts("09:30:05", now)
    assert hs._l2_event_ts("09:30:05", ev) == ev


# ---------------------------------------------------------------------------
# persist_l2_ticks(sqlite, 新形态表)
# ---------------------------------------------------------------------------
def test_persist_event_ts_and_exact_count(monkeypatch):
    eng = _mk_engine(_m158_l2_ticks_event_time_key)
    monkeypatch.setattr(dbs, "engine", eng)
    _patch_now(monkeypatch, datetime(2026, 9, 10, 8, 40, 15, tzinfo=timezone.utc))  # CN 16:40

    rows = [_tick(), _tick(d="s", t="09:30:06", price=10.1, vol=50.0, amt=505.0), "junk"]
    assert hs.persist_l2_ticks("600000", "CN", "thsdk_big_order", rows) == 2
    with eng.connect() as conn:
        got = conn.execute(
            text("SELECT ts, tick_time FROM l2_ticks ORDER BY ts")
        ).all()
    assert [g[1] for g in got] == ["09:30:05", "09:30:06"]
    assert all(str(g[0]).startswith("2026-09-10 09:30:0") for g in got)


def test_persist_replay_idempotent(monkeypatch):
    eng = _mk_engine(_m158_l2_ticks_event_time_key)
    monkeypatch.setattr(dbs, "engine", eng)
    _patch_now(monkeypatch, datetime(2026, 9, 10, 8, 40, 15, tzinfo=timezone.utc))
    rows = [_tick(), _tick(t="09:30:06")]
    assert hs.persist_l2_ticks("600000", "CN", "thsdk_big_order", rows) == 2
    assert hs.persist_l2_ticks("600000", "CN", "thsdk_big_order", rows) == 0
    with eng.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM l2_ticks")).scalar()
    assert n == 2


def test_persist_crossday_same_tick_time_no_collapse(monkeypatch):
    """修复证明: 旧键不含日期, 跨日同秒同价同量会互相顶掉; 新键(ts 含日期)两行共存。"""
    eng = _mk_engine(_m158_l2_ticks_event_time_key)
    monkeypatch.setattr(dbs, "engine", eng)
    _patch_now(monkeypatch, datetime(2026, 9, 10, 8, 40, 15, tzinfo=timezone.utc))
    assert hs.persist_l2_ticks("600000", "CN", "thsdk_big_order", [_tick()]) == 1
    _patch_now(monkeypatch, datetime(2026, 9, 11, 8, 40, 15, tzinfo=timezone.utc))  # 次日同时刻
    assert hs.persist_l2_ticks("600000", "CN", "thsdk_big_order", [_tick()]) == 1
    with eng.connect() as conn:
        n, days = conn.execute(
            text("SELECT COUNT(*), COUNT(DISTINCT substr(ts,1,10)) FROM l2_ticks")
        ).one()
    assert n == 2 and days == 2


def test_persist_after_midnight_maps_prev_day(monkeypatch):
    eng = _mk_engine(_m158_l2_ticks_event_time_key)
    monkeypatch.setattr(dbs, "engine", eng)
    _patch_now(monkeypatch, datetime(2026, 9, 11, 16, 5, 0, tzinfo=timezone.utc))  # CN 00:05 09-12
    assert hs.persist_l2_ticks("600000", "CN", "thsdk_big_order", [_tick(t="15:00:00")]) == 1
    with eng.connect() as conn:
        ts = conn.execute(text("SELECT ts FROM l2_ticks")).scalar()
    assert str(ts).startswith("2026-09-11 15:00:00")


def test_sqlite_retention_delete_still_active(monkeypatch):
    eng = _mk_engine(_m158_l2_ticks_event_time_key)
    monkeypatch.setattr(dbs, "engine", eng)
    _patch_now(monkeypatch, datetime(2026, 9, 10, 8, 40, 15, tzinfo=timezone.utc))
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO l2_ticks (ts, symbol, market, source, direction, price, vol, amt, tick_time) "
                "VALUES ('2020-01-01 00:00:00.000000', '600000', 'CN', 'thsdk_big_order', "
                "'b', 10.0, 100.0, 1000.0, '09:30:05')"
            )
        )
    hs.persist_l2_ticks("600000", "CN", "thsdk_big_order", [_tick()])
    with eng.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM l2_ticks")).scalar()
    assert n == 1  # 2020 旧行被 sqlite in-code retention 清掉


def test_pg_path_skips_incode_delete(monkeypatch):
    """PG 压缩 hypertable 上不跑行级 DELETE(有整事务回滚风险), retention 交给 policy。"""
    eng = _mk_engine(_m158_l2_ticks_event_time_key)
    monkeypatch.setattr(dbs, "engine", eng)
    _patch_now(monkeypatch, datetime(2026, 9, 10, 8, 40, 15, tzinfo=timezone.utc))
    monkeypatch.setattr("src.db.dialect.is_postgres", lambda: True)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO l2_ticks (ts, symbol, market, source, direction, price, vol, amt, tick_time) "
                "VALUES ('2020-01-01 00:00:00.000000', '600000', 'CN', 'thsdk_big_order', "
                "'b', 10.0, 100.0, 1000.0, '09:30:05')"
            )
        )
    hs.persist_l2_ticks("600000", "CN", "thsdk_big_order", [_tick()])
    with eng.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM l2_ticks")).scalar()
    assert n == 2  # 旧行保留, 由 90 天 policy 负责


# ---------------------------------------------------------------------------
# 迁移 v158 / v159
# ---------------------------------------------------------------------------
def _uq_index_sql(conn) -> str:
    row = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='index' AND name='uq_l2_ticks_dedupe'")
    ).scalar()
    return str(row or "")


def test_m158_fresh_creates_event_time_key():
    eng = _mk_engine(_m158_l2_ticks_event_time_key)
    with eng.connect() as conn:
        sql = _uq_index_sql(conn)
    assert "ts" in sql and "tick_time" not in sql


def test_m158_reshapes_empty_old_shape_table():
    def _old_empty(conn):
        _m128_l2_ticks_table(conn)

    eng = _mk_engine(_old_empty)
    with eng.begin() as conn:
        assert "tick_time" in _uq_index_sql(conn)  # 旧形态确认
        _m158_l2_ticks_event_time_key(conn)
        sql = _uq_index_sql(conn)
    assert "ts" in sql and "tick_time" not in sql


def test_m158_noop_when_table_has_data():
    def _old_with_data(conn):
        _m128_l2_ticks_table(conn)
        conn.execute(
            text(
                "INSERT INTO l2_ticks (ts, symbol, market, source, direction, price, vol, amt, tick_time) "
                "VALUES ('2026-09-10 08:40:15', '600000', 'CN', 'thsdk_big_order', "
                "'b', 10.0, 100.0, 1000.0, '09:30:05')"
            )
        )

    eng = _mk_engine(_old_with_data)
    with eng.begin() as conn:
        _m158_l2_ticks_event_time_key(conn)  # 有数据 → no-op(生产走回填脚本)
        sql = _uq_index_sql(conn)
        n = conn.execute(text("SELECT COUNT(*) FROM l2_ticks")).scalar()
    assert "tick_time" in sql and n == 1  # 旧形态原样保留


def test_m159_sqlite_is_noop():
    eng = _mk_engine(lambda conn: None)
    with eng.begin() as conn:
        _m159_klines_minute_continuous_agg(conn)
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE name LIKE 'klines%'")
        ).all()
    assert rows == []
