# -*- coding: utf-8 -*-
"""vendor 调用统计落库(MetricsSink 装饰器)测试。

钉住三件事: ①累计计数真的进了 data_sources(能力矩阵冷启动唯一可用读数);
②窗口内不重复写库(否则热路径写放大); ③落库失败不能把取数打挂, 且计数不丢。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from src.core.md_metrics_sink import DbCountingMetricsSink

DDL = """
CREATE TABLE data_sources (
    id INTEGER PRIMARY KEY,
    provider VARCHAR(32),
    success_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    last_used_at TIMESTAMP
)
"""
LONG = 3600.0     # 窗口内永不自动落库 → 只有手动 flush 会写
IMMEDIATE = 0.0   # 每条 record 都触发落库


@pytest.fixture()
def session_factory():
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        c.execute(text(DDL))
        c.execute(text("INSERT INTO data_sources (id, provider) VALUES (1,'tq'),(2,'tencent'),(3,'broken')"))
    yield sessionmaker(bind=eng)
    eng.dispose()


def _counts(sf, provider):
    db = sf()
    try:
        row = db.execute(text("SELECT success_count, error_count, last_used_at FROM data_sources "
                              "WHERE provider = :p"), {"p": provider}).first()
        return int(row[0]), int(row[1]), row[2]
    finally:
        db.close()


def _rec(sink, vendor, ok=True):
    sink.record(vendor=vendor, datatype="quote", market="CN", ok=ok, count=1,
                latency_ms=80, error="" if ok else "timeout")


def test_manual_flush_writes_cumulative_counts(session_factory):
    sink = DbCountingMetricsSink(flush_sec=LONG, session_factory=session_factory)
    for _ in range(3):
        _rec(sink, "tq")
    _rec(sink, "tq", ok=False)
    _rec(sink, "tencent")
    assert _counts(session_factory, "tq")[:2] == (0, 0)   # 窗口未到 → 一条都没写
    assert sink.flush() == 2                              # 两个 provider 各一次 UPDATE
    assert _counts(session_factory, "tq")[:2] == (3, 1)
    assert _counts(session_factory, "tencent")[:2] == (1, 0)
    assert _counts(session_factory, "tq")[2] is not None  # 打过时间戳
    assert sink.flush() == 0                              # 刷完即空, 不重复自增


def test_successive_flushes_add_not_overwrite(session_factory):
    """多 worker 各自 flush → 必须是 col = col + delta, 不能读改写互相覆盖。"""
    sink = DbCountingMetricsSink(flush_sec=LONG, session_factory=session_factory)
    _rec(sink, "tq")
    sink.flush()
    for _ in range(2):
        _rec(sink, "tq", ok=False)
    sink.flush()
    assert _counts(session_factory, "tq")[:2] == (1, 2)


def test_window_restarts_after_each_flush(session_factory):
    sink = DbCountingMetricsSink(flush_sec=LONG, session_factory=session_factory)
    _rec(sink, "tq")                                       # 窗口内 → 不自动落
    assert _counts(session_factory, "tq")[:2] == (0, 0)
    for _ in range(9):
        _rec(sink, "tq")
    assert _counts(session_factory, "tq")[:2] == (0, 0)
    sink.flush()
    assert _counts(session_factory, "tq")[:2] == (10, 0)


def test_immediate_window_writes_on_every_record(session_factory):
    sink = DbCountingMetricsSink(flush_sec=IMMEDIATE, session_factory=session_factory)
    for _ in range(5):
        _rec(sink, "tq")
    assert _counts(session_factory, "tq")[:2] == (5, 0)


def test_write_failure_does_not_raise_and_keeps_counts(session_factory):
    class BoomSession:
        def execute(self, *a, **k):
            raise RuntimeError("db down")

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    sink = DbCountingMetricsSink(flush_sec=IMMEDIATE, session_factory=lambda: BoomSession())
    _rec(sink, "broken", ok=False)              # record 内部触发 flush, 不得抛出
    assert sink._pending["broken"] == [0, 1]    # 计数回灌, 等下次

    sink._session_factory = session_factory
    assert sink.flush() == 1
    assert _counts(session_factory, "broken")[:2] == (0, 1)


def test_empty_vendor_is_not_counted(session_factory):
    sink = DbCountingMetricsSink(flush_sec=IMMEDIATE, session_factory=session_factory)
    sink.record(vendor="", datatype="quote", market="CN", ok=True, count=1, latency_ms=10)
    assert sink._pending == {}                  # 空 provider 不进待写队列
    assert sink.flush() == 0


def test_flush_at_exit_drains_once(session_factory):
    sink = DbCountingMetricsSink(flush_sec=LONG, session_factory=session_factory)
    _rec(sink, "tq")
    _rec(sink, "tq", ok=False)
    assert sink.flush_at_exit() == 1
    assert sink.flush_at_exit() == 0            # 钩子被重复触发也不会二次自增
    _rec(sink, "tq")
    assert sink.flush_at_exit() == 0            # 已排空 → 后续不再刷
    assert _counts(session_factory, "tq")[:2] == (1, 1)


def test_inmemory_snapshot_semantics_unchanged(session_factory):
    """装饰不能改掉内存快照语义(health()/数据源信任面板依赖它)。"""
    sink = DbCountingMetricsSink(flush_sec=LONG, session_factory=session_factory)
    _rec(sink, "tq")
    _rec(sink, "tq", ok=False)
    snap = sink.snapshot()
    assert snap["tq"]["count"] == 2
    assert snap["tq"]["success_rate"] == 0.5
    assert snap["tq"]["ewma_latency_ms"] is not None
