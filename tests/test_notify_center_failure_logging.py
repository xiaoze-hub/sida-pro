# -*- coding: utf-8 -*-
"""通知中心真实写失败: 日志白名单 + 返回语义不变(2026-11-12 复审四)。

上一轮把 `_notify_refresh_done` 外层补丁改成 `log_failure`, 但**真实失败点**在
`src/core/notify_center.py::push_notification` 内部: 那里 `except Exception` 自己
`logger.exception` 吞掉异常并返回 None, 外层 catch 永远收不到异常 —— 于是
SQLAlchemy 的 `INSERT INTO notifications ...`、`[parameters: (...)]`、DSN 和完整
堆栈仍然原样进服务端日志, 外层补丁只是「看起来修好了」。

本文件不复用 monkeypatch 抛异常的假故障, 而是:
1. 在 tmp_path 下建**真实隔离 SQLite**, `create_all` 后 `DROP TABLE notifications`,
   让 INSERT 触发真实的 sqlalchemy.exc.OperationalError;
2. 把 `notify_center.SessionLocal` 指向该库(只换数据源, 不伪造异常);
3. 分别走 `push_notification` / `notify_task_done`(因子刷新调用链) 两条真实路径,
   用 caplog 断言: 有一个关联号、异常类型、帧位置; 没有 SQL/绑定参数/DSN/token/
   堆栈; 且返回值与不抛异常的语义完全不变;
4. 同一库保留 notifications 表时仍返回真实 id —— 证明改动只影响日志。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import src.web.models  # noqa: F401  注册 ORM 模型
from src.core import notify_center
from src.web.database import Base

# 真实形态的假 secret/DSN/参数: 任何一处泄漏都能被断言抓到。
DB_PASSWORD = "S1da-Pr0d-Pa55w0rd"
DSN = f"postgresql://sida:{DB_PASSWORD}@db.internal:5432/sida"
BODY_SENTINEL = "612-U-NOISE-BODY"
TOKEN_SENTINEL = "bot123456:AAH-fake-token"


def _session_factory(tmp_path: Path, *, with_notifications: bool):
    """tmp_path 下的真实 SQLite + 会话工厂; 可选把 notifications 表删掉。"""
    db_file = tmp_path / f"notify_fail_{DB_PASSWORD}.db"
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    if not with_notifications:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE notifications"))
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _logged(caplog, prefix: str = "src.") -> str:
    """只取本仓库 logger 的日志, 排除 sqlalchemy 等第三方噪声。"""
    return "\n".join(
        rec.getMessage() for rec in caplog.records if rec.name.startswith(prefix)
    )


def _assert_no_leak(text: str, caplog) -> None:
    assert "ERR-" in text, f"缺少关联号: {text!r}"
    assert "OperationalError" in text, f"缺少异常类型: {text!r}"
    assert "frames=" in text, f"缺少帧位置(排障需要): {text!r}"
    for leak in (
        DB_PASSWORD,
        DSN,
        "sqlite:///",
        BODY_SENTINEL,
        TOKEN_SENTINEL,
        "INSERT INTO",
        "parameters",
        "notifications",
        "Traceback",
        "sqlite3.OperationalError",
    ):
        assert leak not in text, f"日志泄漏: {leak!r} in {text!r}"
    # logger.exception 会带 exc_info ⇒ 必须为空, 证明不是「换个函数名」。
    assert all(rec.exc_info is None for rec in caplog.records), "仍带异常堆栈"


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """把通知中心的 SessionLocal 换到隔离 SQLite(真实库, 不是 mock 异常)。"""
    engine, Session = _session_factory(tmp_path, with_notifications=False)
    monkeypatch.setattr(notify_center, "SessionLocal", Session)
    # 断言当前写的不是仓库/生产数据文件。
    assert str(engine.url) != "sqlite:////data/sida.db"
    yield engine
    engine.dispose()


def test_push_notification_real_insert_failure_logs_safely(isolated_db, caplog):
    """真实缺表导致的 INSERT 失败: 安全日志 + 返回 None + 绝不抛。"""
    with caplog.at_level(logging.DEBUG):
        nid = notify_center.push_notification(
            "❌ 因子刷新 执行失败",
            f"{BODY_SENTINEL}; token={TOKEN_SENTINEL}",
            category="strategy",
            level="error",
            link=f"https://example.invalid/hook?token={TOKEN_SENTINEL}",
            source="strategy_signals_refresh",
            user_id="42",
        )

    assert nid is None, "通知失败必须仍返回 None(调用方语义不变)"
    text = _logged(caplog)
    assert "站内通知写入失败" in text
    _assert_no_leak(text, caplog)
    # 只有真实失败点那条; 没有第二条把正文/URL 又打一遍。
    assert text.count("失败") == 1, text


def test_factor_refresh_path_real_failure_stays_in_notify_center(isolated_db, caplog):
    """因子刷新调用链(_notify_refresh_done → notify_task_done)端到端真实失败。

    关键: 外层 `recommendations._notify_refresh_done` 的 log_failure 不应被触发 ——
    异常在 notify_center 内部就被吞成 None, 外层根本看不到。日志必须出自
    notify_center, 且不泄漏 SQL/参数/DSN/token。
    """
    from src.web.api import recommendations

    with caplog.at_level(logging.DEBUG):
        # 不 expect raise: 通知失败绝不顶掉刷新本身。
        recommendations._notify_refresh_done(
            False, "策略信号刷新失败; 参考号 ERR-TEST1234"
        )

    text = _logged(caplog)
    assert "站内通知写入失败" in text
    _assert_no_leak(text, caplog)
    names = {rec.name for rec in caplog.records if rec.levelno >= logging.ERROR}
    assert names == {"src.core.notify_center"}, (
        f"真实失败点应在 notify_center 内部记录, 实际: {names}"
    )
    # 外层补丁不应再记录一遍(它收不到异常), 否则说明内部又把异常抛出去了。
    assert "站内通知写入(策略信号刷新)" not in text


def test_success_path_still_returns_id(tmp_path, monkeypatch, caplog):
    """同一套真实数据源: 表在时仍返回真实 id, 表没了才返回 None。"""
    engine, Session = _session_factory(tmp_path, with_notifications=True)
    monkeypatch.setattr(notify_center, "SessionLocal", Session)
    try:
        with caplog.at_level(logging.DEBUG):
            nid = notify_center.push_notification(
                "✅ 因子刷新 已完成",
                "ok",
                category="strategy",
                level="success",
                source="strategy_signals_refresh",
                user_id="42",
            )
        assert isinstance(nid, int) and nid > 0

        with engine.begin() as conn:
            conn.execute(text("DROP TABLE notifications"))
        caplog.clear()
        with caplog.at_level(logging.DEBUG):
            failed = notify_center.push_notification(
                "✅ 因子刷新 已完成",
                "ok",
                category="strategy",
                level="success",
                source="strategy_signals_refresh",
                user_id="42",
            )
        assert failed is None
        assert "站内通知写入失败" in _logged(caplog)
    finally:
        engine.dispose()

