"""能力矩阵新鲜度缓存（2026-10-31）。

背景：`/api/datasources/capabilities` 每个页面都会调用，内部对 klines 跑 `max(ts)`，
在 1.5GB / 1697 chunks 的 hypertable 上冷态实测 4.6s。数据一天只变一次，故 5 分钟 TTL。
这里锁住三条不变量：命中不再查库、空表也缓存、失败不缓存。
"""

import pytest

from src.core import data_capabilities as dc


class _FakeResult:
    def __init__(self, value):
        self._v = value

    def scalar(self):
        return self._v


class _FakeBind:
    def __init__(self, url):
        self.url = url


class _FakeUrl:
    drivername = "postgresql"
    host = "db"
    database = "sida"


class _FakeDB:
    """记录每次 execute；select 语句直接返回预置值。"""

    def __init__(self, values=None, boom_tables=()):
        self.calls = []
        self._values = values or {}
        self._boom = set(boom_tables)
        self._bind = _FakeBind(_FakeUrl())

    def get_bind(self):
        return self._bind

    def execute(self, stmt):
        q = str(stmt)
        self.calls.append(q)
        for table in self._boom:
            if table in q:
                raise RuntimeError("table missing")
        for table, v in self._values.items():
            if table in q:
                return _FakeResult(v)
        return _FakeResult(None)

    def rollback(self):
        pass


@pytest.fixture(autouse=True)
def _clean_cache():
    dc.clear_freshness_cache()
    yield
    dc.clear_freshness_cache()


def test_second_call_hits_cache_not_db():
    db = _FakeDB(values={"klines": "2026-10-30 15:00:00"})
    first = dc.freshness_probe(db)
    n_first = len(db.calls)
    second = dc.freshness_probe(db)
    assert first == second
    assert len(db.calls) == n_first, "TTL 内不该再查库（klines max() 冷态 4.6s）"
    assert n_first == len(dc.FRESHNESS_PROBES)


def test_expired_entry_is_recomputed():
    db = _FakeDB(values={"klines": "2026-10-30"})
    dc.freshness_probe(db, ttl=0)
    n = len(db.calls)
    dc.freshness_probe(db, ttl=0)
    assert len(db.calls) == 2 * n, "ttl=0 时必须重算"


def test_empty_table_is_cached_as_none():
    db = _FakeDB()
    dc.freshness_probe(db)
    n = len(db.calls)
    out = dc.freshness_probe(db)
    assert out == {}
    assert len(db.calls) == n, "空表也要缓存，否则每次都为'没数据'付一次 max()"


def test_failure_is_not_cached():
    db = _FakeDB(values={"klines": "2026-10-30"}, boom_tables=("klines",))
    dc.freshness_probe(db)
    klines_calls = sum(1 for c in db.calls if "klines" in c)
    dc.freshness_probe(db)
    assert sum(1 for c in db.calls if "klines" in c) == 2 * klines_calls, \
        "查询失败必须重试，不能把'无数据'钉住 5 分钟"


def test_cache_is_partitioned_by_database():
    db1 = _FakeDB(values={"klines": "2026-10-30"})
    db2 = _FakeDB(values={"klines": "2026-10-29"})
    db2._bind = _FakeBind(type("U", (), {"drivername": "postgresql", "host": "db", "database": "other"})())
    dc.freshness_probe(db1)
    out2 = dc.freshness_probe(db2)
    assert "klines" in out2 and str(out2["klines"]).startswith("2026-10-29"), "不同库不能共用缓存"
