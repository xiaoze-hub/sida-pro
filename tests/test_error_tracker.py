"""error_tracker 测试: JSONL 落盘 / 去重 / 高频聚合告警 / 中间件不吞异常。

运行: SIDA_DB_URL="sqlite:////tmp/et.db" python -m pytest tests/test_error_tracker.py -q
"""

import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core import error_tracker
from src.web.database import SessionLocal
from src.web.models import Notification


@pytest.fixture()
def tracker_env(tmp_path):
    """把 error_tracker 指向临时 JSONL 路径并清空进程内状态。"""
    log_file = tmp_path / "error_events.jsonl"
    error_tracker.configure(file_path=str(log_file), reset_state=True)
    yield log_file
    error_tracker._clear_state()


@pytest.fixture()
def throwing_app(tracker_env):
    """构造一个带会抛异常测试路由的最小 FastAPI 应用 + 安装 error_tracker。"""
    import src.core.error_tracker as et

    app = FastAPI()

    @app.get("/boom")
    def boom():
        raise ValueError("boom-value-error")

    @app.get("/ok")
    def ok():
        return {"ok": True}

    et.install_error_tracker(app)
    return app


def _notification_rows(title_prefix: str) -> list[Notification]:
    db = SessionLocal()
    try:
        return (
            db.query(Notification)
            .filter(Notification.title.like(f"{title_prefix}%"))
            .all()
        )
    finally:
        db.close()


def test_capture_writes_jsonl_and_dedupes(tracker_env):
    """同 (type, message) 5 分钟内只落一条, 但每次调用都计数。"""
    err = ValueError("boom-value-error")
    assert error_tracker.capture_exception(err, {"k": 1}) is True
    assert error_tracker.capture_exception(err, {"k": 2}) is False
    assert error_tracker.capture_exception(err, {"k": 3}) is False

    lines = tracker_env.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1  # 去重生效: 只写 1 条

    import json as _json

    data = _json.loads(lines[0])
    assert data["type"] == "ValueError"
    assert data["message"] == "boom-value-error"
    assert "traceback" in data and "ValueError" in data["traceback"]
    assert data["context"] == {"k": 1}


def test_recent_errors_returns_events(tracker_env):
    error_tracker.capture_exception(ValueError("aaa"), {"x": 1})
    error_tracker.capture_exception(RuntimeError("bbb"), {"y": 2})
    events = error_tracker.recent_errors(limit=10)
    assert len(events) == 2  # 不同指纹, 各写一条
    # 最新在前
    assert events[0]["type"] == "RuntimeError"
    assert events[1]["type"] == "ValueError"


def test_install_captures_unhandled_and_rethrows(throwing_app):
    """中间件捕获异常, 但仍正常返回 500(不吞)。"""
    client = TestClient(throwing_app, raise_server_exceptions=False)
    # 正常路径零影响
    assert client.get("/ok").status_code == 200
    # 异常路径: 捕获 → 落盘 → 原样传播返回 500
    resp = client.get("/boom")
    assert resp.status_code == 500


def test_high_frequency_trigger_notification(tracker_env):
    """同一错误 10 分钟内出现 >=3 次 → 写一条 system/error 通知。"""
    from src.web.database import SessionLocal as _SL

    before = _notification_rows("[错误追踪]")

    err = ValueError("boom-value-error")
    for _ in range(3):
        error_tracker.capture_exception(err)

    rows = _notification_rows("[错误追踪]")
    assert len(rows) == len(before) + 1
    n = rows[-1]
    assert n.category == "system"
    assert n.level == "error"
    assert n.title == "[错误追踪] 高频异常: ValueError"
    assert "3 次" in n.body
    assert "boom-value-error" in n.body


def test_high_frequency_throttle_one_per_hour(tracker_env):
    """同指纹每小时最多 1 条通知(防轰炸), 再触发 3 次不重复发。"""
    before = _notification_rows("[错误追踪]")

    err = ValueError("boom-value-error")
    for _ in range(3):
        error_tracker.capture_exception(err)
    rows = _notification_rows("[错误追踪]")
    assert len(rows) == len(before) + 1

    # 同指纹再触发 3 次, 仍在同一小时内 → 不再发新通知
    for _ in range(3):
        error_tracker.capture_exception(err)
    rows2 = _notification_rows("[错误追踪]")
    assert len(rows2) == len(before) + 1


def test_file_write_failure_is_silent():
    """落盘/通知失败绝不影响主流程(capture_exception 永不抛)。"""
    error_tracker.configure(file_path="/nonexistent_dir_xyz/err.jsonl", reset_state=True)
    result = error_tracker.capture_exception(ValueError("x"))
    # 返回 False(未落盘), 但不抛异常
    assert result is False
