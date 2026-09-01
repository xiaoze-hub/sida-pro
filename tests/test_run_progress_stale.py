"""僵尸 running 检测单测 — server 重启 / 任务死掉后,前端 polling 应能被告知 stale。

S5 归属校验(2026-08-26)后 get_run_progress 查询顺序:
  1. db.query(AgentRun)  归属校验(owner_run, first())
  2. db.query(LogEntry)  日志聚合(all())
  3. db.query(AgentRun)  取 run 最终状态(first())
本测试按此顺序配置 MagicMock, 归属校验默认放行(owner_run 返回 fake run)。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch


def _fake_log(timestamp, message="stage:market_analyst", tags=None):
    le = MagicMock()
    le.id = 1
    le.timestamp = timestamp
    le.level = "INFO"
    le.message = message
    le.tags = tags or {}
    le.event = "ta_progress"
    le.trace_id = "trace-stale-test"
    return le


def _db_with(logs, run=None, owner=None) -> MagicMock:
    """按 get_run_progress 的 3 次 query 顺序配置 MagicMock db。

    owner: 归属校验返回(默认 fake run, 放行); run: 最终 run(first, 默认 None)。
    """
    db = MagicMock()
    owner_query = MagicMock()
    owner_query.filter.return_value.order_by.return_value.first.return_value = (
        owner if owner is not None else MagicMock()
    )
    log_query = MagicMock()
    log_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = logs
    run_query = MagicMock()
    run_query.filter.return_value.order_by.return_value.first.return_value = run
    db.query.side_effect = [owner_query, log_query, run_query]
    return db


def test_recent_log_status_running():
    """最近 1 分钟内有日志 → status=running"""
    from src.web.api.agents import get_run_progress

    now = datetime.now(timezone.utc)
    recent = _fake_log(now - timedelta(seconds=30))
    db = _db_with([recent], run=None)

    with patch("src.agents.tradingagents.progress.aggregate_progress", return_value={"stages": []}):
        result = get_run_progress("trace-stale-test", db, user=MagicMock(id=1))
    assert result["status"] == "running"


def test_old_log_status_stale():
    """最后日志距今 > 5 分钟 → status=stale(server 重启 / 进程死掉)"""
    from src.web.api.agents import get_run_progress

    now = datetime.now(timezone.utc)
    old = _fake_log(now - timedelta(minutes=10))
    db = _db_with([old], run=None)

    with patch("src.agents.tradingagents.progress.aggregate_progress", return_value={"stages": []}):
        result = get_run_progress("trace-stale-test", db, user=MagicMock(id=1))
    assert result["status"] == "stale"


def test_no_logs_status_not_found():
    """没日志没 run → status=not_found"""
    from src.web.api.agents import get_run_progress

    db = _db_with([], run=None)

    with patch("src.agents.tradingagents.progress.aggregate_progress", return_value={"stages": []}):
        result = get_run_progress("trace-stale-test", db, user=MagicMock(id=1))
    assert result["status"] == "not_found"


def test_run_completed_overrides_log_status():
    """有 AgentRun 完成记录时,以 run.status 为准,不再判 stale"""
    from src.web.api.agents import get_run_progress

    now = datetime.now(timezone.utc)
    old_log = _fake_log(now - timedelta(minutes=20))

    fake_run = MagicMock()
    fake_run.agent_name = "tradingagents"
    fake_run.status = "success"
    fake_run.result = "ok"
    fake_run.error = ""
    fake_run.duration_ms = 180000
    fake_run.model_label = "deepseek-chat"
    fake_run.notify_sent = True

    db = _db_with([old_log], run=fake_run)

    with patch("src.agents.tradingagents.progress.aggregate_progress", return_value={"stages": []}):
        result = get_run_progress("trace-stale-test", db, user=MagicMock(id=1))
    assert result["status"] == "success"


def test_not_owner_trace_not_found():
    """他人 trace(非当前用户) → status=not_found(归属校验拒绝, 不泄露存在性)"""
    from src.web.api.agents import get_run_progress

    db = MagicMock()
    owner_query = MagicMock()
    owner_query.filter.return_value.order_by.return_value.first.return_value = None  # 无归属记录
    db.query.side_effect = [owner_query]

    with patch("src.agents.tradingagents.progress.aggregate_progress", return_value={"stages": []}):
        result = get_run_progress("trace-not-owner", db, user=MagicMock(id=1))
    assert result["status"] == "not_found"
