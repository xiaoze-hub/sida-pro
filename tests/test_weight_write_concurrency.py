# -*- coding: utf-8 -*-
"""权重写入的并发一致性(2026-11-12 复审二)。

审计问题:
    `set_factor_weight` / `rebalance_strategy_weights` 都是「读旧值 → 写新值 →
    写历史」。这不是一条原子语句: 两笔并发若都读到 1.0, 历史会写成
    `1.0→1.2` + `1.0→1.8` —— 后一笔的 before 是错的, 历史链断开,
    `get_factor_weights(as_of=...)` 回放会把中间那次改动整段吞掉。

本文件验证:
1. 两线程并发改同一因子权重, 历史链仍然首尾相接(进程内锁生效);
2. 读行时真的用了 `FOR UPDATE`(PG/MySQL 行锁是跨进程的那一半);
3. 策略调权把「目录读取」放进**同一个写会话/事务**, 且整段持锁。
"""

from __future__ import annotations

import threading
import time

import pytest

DB_ENV_KEYS = (
    "DATABASE_URL",
    "DATABASE_URL_WRITE",
    "DATABASE_URL_READ",
    "DATABASE_URL_MIGRATE",
    "SIDA_DB_URL",
    "SIDA_DB",
    "SIDA_APP_DB_ROLE",
)


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch):
    """清掉可能指向真实库的环境变量, 并断言用的是隔离 SQLite。"""
    for key in DB_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    from src.db.session import write_engine

    url = str(write_engine.url)
    assert url.startswith("sqlite"), f"测试库必须是隔离 SQLite, 实际: {url}"
    assert "/data/sida.db" not in url


@pytest.fixture()
def clean_weights():
    """清掉本文件关心的权重/历史/审计行(用例前后各一次)。"""
    from src.web.database import SessionLocal
    from src.web.models import (
        AuditLog,
        FactorWeight,
        FactorWeightHistory,
        StrategyWeight,
        StrategyWeightHistory,
    )

    def _wipe():
        db = SessionLocal()
        try:
            db.query(FactorWeightHistory).delete()
            db.query(FactorWeight).delete()
            db.query(StrategyWeightHistory).delete()
            db.query(StrategyWeight).delete()
            db.query(AuditLog).filter(
                AuditLog.action.in_(
                    ("update_factor_weight", "rebalance_strategy_weights")
                )
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()

    _wipe()
    yield
    _wipe()


def _history_rows():
    from src.web.database import SessionLocal
    from src.web.models import FactorWeightHistory

    db = SessionLocal()
    try:
        return (
            db.query(FactorWeightHistory)
            .order_by(FactorWeightHistory.id.asc())
            .all()
        )
    finally:
        db.close()


# ── 1. 并发改同一因子: 历史链不得断 ────────────────────────────────────
def test_concurrent_factor_weight_writes_keep_history_chain(clean_weights, monkeypatch):
    from src.core import factor_weights as fw

    # 首笔写入会补默认行(1.0):
    fw.set_factor_weight("alpha_score", "CN", weight=1.0, actor="alice")

    real_loader = fw._load_row_for_update

    def _slow_loader(db, factor_code, market):
        # 放大「读到旧值」与「写新值」之间的窗口: 没有锁时两线程都会读到同一个旧值。
        time.sleep(0.05)
        return real_loader(db, factor_code, market)

    monkeypatch.setattr(fw, "_load_row_for_update", _slow_loader)

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _worker(target_weight: float) -> None:
        try:
            barrier.wait(timeout=10)
            fw.set_factor_weight(
                "alpha_score", "CN", weight=target_weight, actor="alice"
            )
        except BaseException as exc:  # pragma: no cover - 线程里只记录
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(w,)) for w in (1.2, 1.8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors, errors

    hist = _history_rows()
    assert len(hist) == 2, [ (h.old_weight, h.new_weight) for h in hist ]
    pairs = [(round(h.old_weight, 4), round(h.new_weight, 4)) for h in hist]
    assert pairs[0][0] == 1.0
    assert pairs[1][0] == pairs[0][1], (
        f"历史链断了(后一笔 before 不等于前一笔 after): {pairs} —— "
        "并发写入读到了同一个旧值"
    )
    assert {pairs[0][1], pairs[1][1]} == {1.2, 1.8}


# ── 2. 读行真的加了行锁 ────────────────────────────────────────────────
def test_load_row_for_update_uses_for_update(monkeypatch):
    """`_load_row_for_update` 必须走 `FOR UPDATE` + `populate_existing()`。

    SQLite 编译器会把 FOR UPDATE 丢掉, 光看 SQL 文本验不出来, 所以这里用
    spy 记录 Query 上是否真的调过 `with_for_update()`。

    `populate_existing()`(2026-11-12 复审三)同样是必须的: FOR UPDATE 只保证
    SELECT 拿到最新行, 属性仍可能来自会话身份映射里的旧对象 —— 少了它, 历史里的
    before 就是会话缓存值(真实 PG 上实测 1.0 vs 库里 1.7)。
    """
    from src.core import factor_weights as fw

    calls = {"for_update": 0, "populate_existing": 0}

    class _SpyQuery:
        def __init__(self, real):
            self._real = real

        def filter(self, *args, **kwargs):
            self._real = self._real.filter(*args, **kwargs)
            return self

        def populate_existing(self, *args, **kwargs):
            calls["populate_existing"] += 1
            self._real = self._real.populate_existing(*args, **kwargs)
            return self

        def with_for_update(self, *args, **kwargs):
            calls["for_update"] += 1
            self._real = self._real.with_for_update(*args, **kwargs)
            return self

        def first(self):
            return self._real.first()

    class _SpySession:
        def __init__(self, real):
            self._real = real

        def query(self, *args, **kwargs):
            return _SpyQuery(self._real.query(*args, **kwargs))

    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        fw._load_row_for_update(_SpySession(db), "alpha_score", "CN")
    finally:
        db.close()
    assert calls["for_update"] == 1, "读权重行没有加行锁"
    assert calls["populate_existing"] == 1, "读权重行没有用 SELECT 结果覆盖会话里的旧对象"


# ── 3. 调权: 目录读进同一事务 + 全段持锁 ────────────────────────────────
def test_rebalance_reads_catalog_in_same_write_transaction(clean_weights, monkeypatch):
    from src.core import strategy_engine as se

    seen: dict = {}
    real_ensure = se.ensure_strategy_catalog
    real_list = se.list_strategy_catalog
    entered = threading.Event()
    release = threading.Event()

    def _traced_ensure(*args, **kwargs):
        seen["ensure_db"] = kwargs.get("db")
        return real_ensure(*args, **kwargs)

    def _traced_list(*args, **kwargs):
        seen["list_db"] = kwargs.get("db")
        entered.set()
        assert release.wait(10), "测试未放行调权"
        return real_list(*args, **kwargs)

    monkeypatch.setattr(se, "ensure_strategy_catalog", _traced_ensure)
    monkeypatch.setattr(se, "list_strategy_catalog", _traced_list)

    result: dict = {}

    def _run():
        result.update(se.rebalance_strategy_weights(actor="owner_test"))

    t = threading.Thread(target=_run)
    t.start()
    try:
        assert entered.wait(10), "调权没走到目录读取"
        # 目录读取用的会话必须与 ensure 同一个(即同一个写事务), 不是另开的读会话。
        assert seen.get("list_db") is not None, "调权仍在用独立会话读目录"
        assert seen.get("list_db") is seen.get("ensure_db"), (
            "目录补行与目录读取不在同一个会话/事务里"
        )
        # 调权期间另一线程拿不到锁 ⇒ 读改写确实被串行化。
        acquired = se._REBALANCE_LOCK.acquire(blocking=False)
        if acquired:
            se._REBALANCE_LOCK.release()
        assert acquired is False, "调权没有持锁, 并发时历史链会断"
    finally:
        release.set()
        t.join(30)

    assert not result.get("error"), result
