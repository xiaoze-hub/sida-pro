"""`_load_latest_archived()` 的会话生命周期与降级契约(2026-10-22 代码审查)。

被审查的真问题(Medium): 原实现手写 `db = SessionLocal()` + `finally: db.close()`,
异常路径容易漏关会话, 且不符合仓内 SQLAlchemy 2.0 惯用法
(`src/core/limit_ladder_live.py:312`、`src/core/klines_fullmarket.py:47` 都是
`with SessionLocal() as db:`)。另外函数顶部有一句从未使用的
`from src.core.trade_phase import cn_now`。

本文件锁死三件事:
1. 读不到数据(表空 / 建会话即抛) ⇒ 返 `(None, [])` 且**绝不向外抛** —— 这是旁路;
2. 正常读到行 ⇒ 返 `("YYYY-MM-DD", [ {symbol,name,gap_pct,withdraw_rate,volume_ratio,note} ])`;
3. 会话**必定关闭**, 且关闭靠的是上下文管理器(不是手写 finally)。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from src.core import auction_pool

DAY = datetime(2026, 10, 20, 9, 25, 3)


# ── 假会话: 只实现上下文管理器协议, 故意**不**提供 .close() ──────────────────
class _FakeQuery:
    """链式 query 的最小替身: 调用序无关, 全部返回自身。"""

    def __init__(self, last, rows):
        self._last = last
        self._rows = rows

    def order_by(self, *a, **kw):
        return self

    def filter(self, *a, **kw):
        return self

    def limit(self, *n):
        return self

    def first(self):
        return self._last

    def all(self):
        return self._rows


class _CtxOnlySession:
    """只有 `__enter__`/`__exit__` 的会话替身。

    旧实现 `db = SessionLocal(); ...; finally: db.close()` 在这个对象上会抛
    AttributeError(没有 .close), 被外层降级 except 吞掉 ⇒ 返 `(None, [])`;
    新实现 `with SessionLocal() as db:` 正常走完并拿到数据 —— 这条用例因此
    能把"手写 close"和"上下文管理器"区分开。
    """

    def __init__(self, last=None, rows=()):
        self._last = last
        self._rows = list(rows)
        self.entered = False
        self.exited = False
        self.exit_exc_type = None

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exited = True
        self.exit_exc_type = exc_type
        return False  # 不吞异常: 有异常必须继续冒到外层降级分支

    def query(self, model):
        return _FakeQuery(self._last, self._rows)


def _row(**kw):
    base = dict(
        symbol="600111",
        name="北方稀土",
        gap_pct=3.2,
        withdraw_rate=0.243,
        volume_ratio=None,
        note="大幅高开",
        created_at=DAY,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _patch_session_local(monkeypatch, factory):
    """函数体内 `from src.db.session import SessionLocal`, 所以打在模块属性上。"""
    from src.db import session as db_session

    monkeypatch.setattr(db_session, "SessionLocal", factory)


# ── ① 读不到数据 ⇒ (None, []) 且不抛 ──────────────────────────────────────
def test_empty_table_returns_none_and_empty_without_raising(monkeypatch):
    sess = _CtxOnlySession(last=None, rows=[])
    _patch_session_local(monkeypatch, lambda: sess)

    day, records = auction_pool._load_latest_archived()

    assert day is None
    assert records == []
    assert sess.entered is True, "必须通过上下文管理器进入会话"
    assert sess.exited is True, "空表提前 return 时也必须关闭会话"
    assert sess.exit_exc_type is None


def test_session_construction_failure_degrades_silently(monkeypatch):
    def _boom():
        raise RuntimeError("database is locked")

    _patch_session_local(monkeypatch, _boom)

    # 不抛: 这是旁路, 读不到归档就当没有归档
    day, records = auction_pool._load_latest_archived()

    assert (day, records) == (None, [])


def test_query_failure_degrades_silently(monkeypatch):
    class _ExplodingSession(_CtxOnlySession):
        def query(self, model):
            raise RuntimeError("no such table: auction_anomaly_records")

    sess = _ExplodingSession()
    _patch_session_local(monkeypatch, lambda: sess)

    day, records = auction_pool._load_latest_archived()

    assert (day, records) == (None, [])
    assert sess.exited is True, "查询抛错时上下文管理器仍须关闭会话"


# ── ② 正常读到行 ⇒ (日期字符串, 列表), 字段名不变 ─────────────────────────
def test_rows_return_date_string_and_contract_fields(monkeypatch):
    rows = [
        _row(symbol="600111", name="北方稀土", gap_pct=3.2),
        _row(symbol="002361", name="神剑股份", gap_pct=-1.2, withdraw_rate=0.1,
             volume_ratio=2.5, note="跌停试盘"),
    ]
    sess = _CtxOnlySession(last=rows[0], rows=rows)
    _patch_session_local(monkeypatch, lambda: sess)

    day, records = auction_pool._load_latest_archived()

    assert day == "2026-10-20"
    assert isinstance(day, str)
    assert len(records) == 2
    assert set(records[0]) == {
        "symbol", "name", "gap_pct", "withdraw_rate", "volume_ratio", "note",
    }, "返回字段名一个都不许改"
    assert records[1]["symbol"] == "002361"
    assert records[1]["gap_pct"] == pytest.approx(-1.2)
    assert records[1]["note"] == "跌停试盘"
    assert sess.exited is True


# ── ③ 会话必定关闭(真实 Session + 真实内存库) ─────────────────────────────
@pytest.fixture
def real_session_factory(monkeypatch):
    """真 Session 子类 + 内存 SQLite: 记录 close() 是否被调用、共创建几个会话。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from src.db.models import AuctionAnomalyRecord
    from src.db.session import Base, Session

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)

    created: list = []

    class _RecordingSession(Session):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.close_calls = 0
            created.append(self)

        def close(self):
            self.close_calls += 1
            super().close()

    factory = sessionmaker(bind=engine, class_=_RecordingSession, expire_on_commit=False)
    _patch_session_local(monkeypatch, factory)

    def _write(rows):
        with factory() as s:  # fixture 自己写入不走被记录的断言路径
            for r in rows:
                s.add(AuctionAnomalyRecord(**r))
            s.commit()
        created.clear()  # 只统计被 _load_latest_archived 创建的会话

    yield _write, created

    engine.dispose()


def test_context_manager_closes_real_session(real_session_factory):
    write, created = real_session_factory
    write([
        dict(symbol="600111", name="北方稀土", gap_pct=1.0, note="", created_at=DAY),
        dict(symbol="002361", name="神剑股份", gap_pct=9.9, note="", created_at=DAY),
    ])

    day, records = auction_pool._load_latest_archived()

    assert day == "2026-10-20"
    # 按 gap_pct desc 排序语义不变
    assert [r["symbol"] for r in records] == ["002361", "600111"]
    assert len(created) >= 1, "应通过 SessionLocal 创建会话"
    assert all(s.close_calls >= 1 for s in created), "上下文管理器退出必须关闭会话"


def test_limit_semantics_unchanged(real_session_factory):
    write, _created = real_session_factory
    write([
        dict(symbol=f"60000{i}", name=f"票{i}", gap_pct=float(i), note="", created_at=DAY)
        for i in range(5)
    ])

    _day, records = auction_pool._load_latest_archived(limit=2)
    assert len(records) == 2

    _day, all_rows = auction_pool._load_latest_archived()
    assert len(all_rows) == 5


def test_real_empty_table_returns_none(real_session_factory):
    _write, _created = real_session_factory  # 建表但不插数据
    day, records = auction_pool._load_latest_archived()
    assert (day, records) == (None, [])


def test_single_day_window_excludes_neighbours(real_session_factory):
    write, _created = real_session_factory
    write([
        dict(symbol="600001", name="前一日", gap_pct=5.0, note="", created_at=DAY - timedelta(days=1)),
        dict(symbol="600002", name="当日", gap_pct=4.0, note="", created_at=DAY),
    ])
    day, records = auction_pool._load_latest_archived()
    assert day == "2026-10-20"
    assert [r["symbol"] for r in records] == ["600002"]
