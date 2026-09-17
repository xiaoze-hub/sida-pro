"""P1 稳定性改进测试: Loki / APM / 读写分离 / 慢接口异步化。

覆盖:
- loki_logger: 未配置不影响; 配置后 handler 幂等挂载; emit 入队
- apm: trace_id / span / record_op / is_read_sql
- dialect: 读写分离 URL 解析, 向后兼容(无副本时行为不变)
- session: RoutingSession 无副本时与普通 Session 等价
- market_data: 后台任务单飞 + status 端点存在
"""
from __future__ import annotations

import logging
import os
import time


# ──────────── Loki ────────────

def test_loki_disabled_by_default(monkeypatch):
    monkeypatch.delenv("LOKI_URL", raising=False)
    from src.core import loki_logger

    assert loki_logger.loki_enabled() is False
    assert loki_logger.ensure_loki_handler() is None
    stats = loki_logger.get_loki_stats()
    assert stats["enabled"] is False


def test_loki_handler_enqueue_and_idempotent(monkeypatch):
    monkeypatch.setenv("LOKI_URL", "http://127.0.0.1:9/loki")  # 不可达, 推送失败静默
    from src.core import loki_logger

    root = logging.getLogger("test_loki_root")
    root.setLevel(logging.DEBUG)
    # 清理旧 handler
    for h in list(root.handlers):
        root.removeHandler(h)

    h1 = loki_logger.ensure_loki_handler(root)
    assert h1 is not None
    h2 = loki_logger.ensure_loki_handler(root)
    assert h1 is h2  # 幂等

    logger = logging.getLogger("test_loki_root.biz")
    logger.info("hello loki")
    # emit 只入队
    with h1._lock:
        assert len(h1._queue) >= 1
        ns, labels, line = h1._queue[-1]
    assert labels["job"] == "sida"
    assert labels["level"] == "INFO"
    assert "test_loki_root.biz" in labels["module"]

    stats = h1.stats()
    assert stats["enabled"] is True
    assert stats["pending"] >= 1

    # 清理
    root.removeHandler(h1)
    h1.close()
    monkeypatch.delenv("LOKI_URL", raising=False)


def test_loki_module_label_truncate():
    from src.core.loki_logger import _module_label, _MODULE_LABEL_MAX

    assert _module_label("src.web.api") == "src.web.api"
    long = "x" * 100
    assert len(_module_label(long)) == _MODULE_LABEL_MAX
    assert _module_label("") == "unknown"


# ──────────── APM ────────────

def test_apm_trace_id_scope():
    from src.core import apm

    assert apm.get_trace_id() == ""
    with apm.trace_scope("abc123") as tid:
        assert tid == "abc123"
        assert apm.get_trace_id() == "abc123"
    assert apm.get_trace_id() == ""

    with apm.trace_scope() as tid:
        assert len(tid) == 16


def test_apm_record_op_and_stats():
    from src.core import apm

    apm.record_op("db", "select:users", 5.0)
    apm.record_op("http", "example.com", 1500.0)  # 慢
    stats = apm.get_apm_stats()
    assert stats["enabled"] is True
    assert stats["ops"]["db"]["count"] >= 1
    assert stats["ops"]["http"]["slow"] >= 1


def test_apm_span_records_error():
    from src.core import apm

    try:
        with apm.span("custom", "boom"):
            raise ValueError("x")
    except ValueError:
        pass
    stats = apm.get_apm_stats()
    assert stats["ops"]["custom"]["errors"] >= 1


def test_is_read_sql_routing():
    from src.db.dialect import is_read_sql

    assert is_read_sql("SELECT * FROM users") is True
    assert is_read_sql("select id from stocks where x=1") is True
    assert is_read_sql("SELECT * FROM users FOR UPDATE") is False
    assert is_read_sql("INSERT INTO t (a) VALUES (1)") is False
    assert is_read_sql("UPDATE t SET a=1") is False
    assert is_read_sql("DELETE FROM t") is False
    assert is_read_sql("") is False
    assert is_read_sql(None) is False
    assert is_read_sql("WITH x AS (SELECT 1) SELECT * FROM x") is True
    # 保守: CTE 混 DML 当写
    assert is_read_sql("WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x") is False


# ──────────── 读写分离 dialect ────────────

def test_dialect_single_db_backward_compat(monkeypatch):
    """未配置 DATABASE_URL_READ 时, 读 URL 回落写 URL, has_read_replica=False。"""
    monkeypatch.delenv("DATABASE_URL_READ", raising=False)
    monkeypatch.delenv("DATABASE_URL_WRITE", raising=False)
    # 直接测函数逻辑(模块级常量已烤死, 用 reload 不安全; 改测纯函数语义)
    from src.db import dialect

    # 当前测试进程未配 READ → has_read_replica False
    assert dialect.has_read_replica() is False
    assert dialect.read_db_url() == dialect.DATABASE_URL_WRITE
    # engine 单库: session.read_engine is write_engine
    from src.db import session as db_session

    assert db_session.read_engine is db_session.write_engine
    assert db_session.engine is db_session.write_engine


def test_dialect_has_read_replica_logic(monkeypatch):
    """has_read_replica 纯逻辑: 同 URL 未分离, 不同 URL 已分离, 空未分离。"""
    from src.db import dialect

    monkeypatch.setattr(dialect, "DATABASE_URL_WRITE", "postgresql://w")
    monkeypatch.setattr(dialect, "DATABASE_URL_READ", None)
    assert dialect.has_read_replica() is False

    monkeypatch.setattr(dialect, "DATABASE_URL_READ", "postgresql://w")
    assert dialect.has_read_replica() is False  # 同 URL 视为未分离

    monkeypatch.setattr(dialect, "DATABASE_URL_READ", "postgresql://r")
    assert dialect.has_read_replica() is True
    assert dialect.read_db_url() == "postgresql://r"


def test_session_routing_single_db_uses_same_engine():
    """单库模式 RoutingSession 行为: SELECT 也走 write_engine(同一对象)。"""
    from src.db.session import SessionLocal, read_engine, write_engine

    assert read_engine is write_engine
    db = SessionLocal()
    try:
        bind = db.get_bind(clause=None)
        assert bind is write_engine
    finally:
        db.close()


def test_get_db_readonly_yields_session():
    from src.db.session import get_db

    gen = get_db(readonly=True)
    db = next(gen)
    try:
        assert db is not None
    finally:
        gen.close()


# ──────────── 慢接口后台任务 ────────────

def test_bg_job_single_flight_and_status():
    from src.web.api import market_data as md

    calls = []
    started = []

    def _fn():
        calls.append(1)
        time.sleep(0.15)
        return {"ok": True}

    key = f"test_bg_{time.time()}"
    r1 = md._bg_start(key, _fn)
    r2 = md._bg_start(key, _fn)  # 单飞, 不重复起
    assert r1 is True
    assert r2 is False

    st_running = md._bg_job_status(key)
    assert st_running["status"] in ("running", "succeeded")

    # 等完成
    deadline = time.time() + 2
    while time.time() < deadline:
        if md._bg_job_status(key)["status"] == "succeeded":
            break
        time.sleep(0.05)
    st = md._bg_job_status(key)
    assert st["status"] == "succeeded"
    assert len(calls) == 1  # 只跑了一次


def test_bg_job_failure():
    from src.web.api import market_data as md

    def _fn():
        raise RuntimeError("boom")

    key = f"test_bg_fail_{time.time()}"
    md._bg_start(key, _fn)
    deadline = time.time() + 2
    while time.time() < deadline:
        if md._bg_job_status(key)["status"] == "failed":
            break
        time.sleep(0.05)
    st = md._bg_job_status(key)
    assert st["status"] == "failed"
    assert "boom" in st["error"]


def test_bg_job_status_idle():
    from src.web.api import market_data as md

    st = md._bg_job_status("never_existed_key_xyz")
    assert st["status"] == "idle"


def test_breadth_status_endpoint_registered():
    """路由存在(不真正发 HTTP, 只查 router)。"""
    from src.web.api.market_data import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/breadth-distribution/status" in paths
    assert "/breadth-distribution" in paths
    assert "/dragon-tiger/range/status" in paths
    assert "/dragon-tiger/{trade_date}/status" in paths


def test_empty_breadth_shape():
    from src.web.api.market_data import _empty_breadth, _BUCKET_BOUNDS

    empty = _empty_breadth()
    assert empty["pending"] is True
    assert len(empty["items"]) == len(_BUCKET_BOUNDS)
    assert all(i["count"] == 0 for i in empty["items"])


# ──────────── 集成: session 可用 ────────────

def test_session_local_still_works():
    """回归: SessionLocal 仍能建会话并跑一条简单查询(SQLite)。"""
    from sqlalchemy import text

    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(text("SELECT 1 AS x")).fetchall()
        assert rows[0][0] == 1
    finally:
        db.close()
