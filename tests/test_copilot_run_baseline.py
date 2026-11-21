"""7/30 天运行基线（只读聚合）与阶段打点（2026-11-21）。

要解决的问题：治理页只有「N 次运行 / 一个平均耗时」，看不出某个场景在 7 天或
30 天窗口里是**没跑过**、**跑了但测不到耗时**，还是**真的变慢了**；也分不清
超时、工具全败、预算中止。这里把可用观测口径钉死：

- 每场景 n + 状态分布 + P50/P90（nearest-rank，小样本不插值）；
- 只用 `copilot_runs` / `copilot_steps` 既有字段，**不迁移模型、不补历史**；
- 没测到写 None 不写 0（0ms 与「没测」是两件事）；
- 工具耗时**逐条累加**只作参考，绝不冒充关键路径；
- TTFT / 排队等待 / 阶段耗时没有落库事实源 → 明确标 available=False，不拿
  「非流式结束」冒充首字。

阶段打点只写结构化低敏日志（run_id/stage/ms/ok），不落用户问题与工具参数。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, CopilotRun, CopilotStep


def _row(*, recipe="deep_diagnosis", status="ok", elapsed_ms=1000, error=None,
         tool_calls=2, run_id=None, conversation_id=1, dispatch_reason=None,
         created_at=None):
    return SimpleNamespace(
        run_id=run_id or str(uuid.uuid4()),
        recipe=recipe,
        status=status,
        elapsed_ms=elapsed_ms,
        error=error,
        tool_calls=tool_calls,
        conversation_id=conversation_id,
        dispatch_reason=dispatch_reason,
        created_at=created_at or datetime.utcnow(),
    )


def _steps(*durations, ok=True):
    return [SimpleNamespace(ok=ok, elapsed_ms=d) for d in durations]


# ── 纯聚合 ────────────────────────────────────────────────────────────────
def test_percentile_nearest_rank_small_samples():
    from src.web.api.copilot import percentile_nearest_rank

    assert percentile_nearest_rank([], 0.5) is None
    assert percentile_nearest_rank([400], 0.5) == 400
    assert percentile_nearest_rank([100, 200, 300, 400], 0.5) == 200
    assert percentile_nearest_rank([100, 200, 300, 400], 0.9) == 400
    # 10 个样本: P90 取第 9 个, 不是最大值(nearest-rank 的确定口径)
    assert percentile_nearest_rank(list(range(1, 11)), 0.9) == 9


def test_empty_window_is_not_zero_latency():
    from src.web.api.copilot import build_copilot_run_baseline

    data = build_copilot_run_baseline([], days=7)

    assert data["days"] == 7
    assert data["runs"] == 0
    assert data["by_scenario"] == []
    # 没数据 = 没测到, 不是 0ms / 0% 成功
    assert data["latency"]["p50_elapsed_ms"] is None
    assert data["latency"]["p90_elapsed_ms"] is None
    assert data["latency"]["n_measured"] == 0
    assert data["ok_rate"] is None
    assert data["coverage"]["elapsed_measured"] == {"n": 0, "of": 0, "ratio": None}


def test_by_scenario_counts_status_and_latency():
    from src.web.api.copilot import build_copilot_run_baseline

    rows = [
        _row(recipe="deep_diagnosis", status="ok", elapsed_ms=1000),
        _row(recipe="deep_diagnosis", status="degraded", elapsed_ms=3000),
        _row(recipe="deep_diagnosis", status="error", elapsed_ms=2000, error="x"),
        _row(recipe=None, status="ok", elapsed_ms=500),
        _row(recipe=None, status="ok", elapsed_ms=None),
    ]
    data = build_copilot_run_baseline(rows, days=7)
    by = {s["scenario"]: s for s in data["by_scenario"]}

    assert set(by) == {"deep_diagnosis", "(默认)"}
    deep = by["deep_diagnosis"]
    assert deep["n"] == 3
    assert deep["status_counts"]["ok"] == 1
    assert deep["status_counts"]["degraded"] == 1
    assert deep["status_counts"]["error"] == 1
    # [1000,2000,3000] → P50 中位=2000, P90=3000
    assert deep["p50_elapsed_ms"] == 2000
    assert deep["p90_elapsed_ms"] == 3000
    assert deep["n_measured"] == 3

    default = by["(默认)"]
    assert default["n"] == 2
    assert default["n_measured"] == 1, "elapsed_ms 缺失的行不得进分位数"
    assert default["p50_elapsed_ms"] == 500
    assert default["p90_elapsed_ms"] == 500


def test_unmeasured_latency_excluded_from_percentiles():
    from src.web.api.copilot import build_copilot_run_baseline

    rows = [_row(elapsed_ms=None), _row(elapsed_ms=None)]
    data = build_copilot_run_baseline(rows, days=30)

    assert data["runs"] == 2
    assert data["latency"]["n_measured"] == 0
    assert data["latency"]["p50_elapsed_ms"] is None
    assert data["coverage"]["elapsed_measured"] == {"n": 0, "of": 2, "ratio": 0.0}


def test_tools_sum_is_not_reported_as_critical_path():
    from src.web.api.copilot import build_copilot_run_baseline

    rows = [_row(elapsed_ms=1200, tool_calls=2)]
    steps = {rows[0].run_id: _steps(3000, 4000)}
    data = build_copilot_run_baseline(rows, days=7, steps_by_run=steps)

    tools = data["tools"]
    assert tools["n_steps"] == 2
    assert tools["sum_ms"] == 7000
    # 关键路径只能来自 run 级墙钟, 不能被 7s 的工具累加替换
    assert data["latency"]["p50_elapsed_ms"] == 1200
    assert data["latency"]["p50_elapsed_ms"] != tools["sum_ms"]
    assert "关键路径" in tools["note"]


@pytest.mark.parametrize("error,expected", [
    ("failed_tools=2/5; first_error=get_kline: timeout", "tool_failure"),
    ("超时：超过 600s 未完成，已转后台失败（可重试）", "timeout"),
    ("工具调用预算超限：已用 12 次 > 上限 10 次，已中止本轮", "budget_exceeded"),
    ("用户取消", "cancelled"),
    ("RuntimeError: 配方 'x' 不存在", "execution_error"),
    (None, "unknown"),
    ("", "unknown"),
])
def test_degradation_cause_classification(error, expected):
    from src.web.api.copilot import classify_degradation_cause

    assert classify_degradation_cause(error=error) == expected


def test_degradation_causes_counted_per_scenario_and_overall():
    from src.web.api.copilot import build_copilot_run_baseline

    rows = [
        _row(status="error", error="failed_tools=1/1; first_error=get_kline: boom"),
        _row(status="failed", error="超时：超过 600s 未完成"),
        _row(status="cancelled", error="用户取消"),
        _row(status="ok"),
    ]
    data = build_copilot_run_baseline(rows, days=7)
    assert data["degradation_causes"]["tool_failure"] == 1
    assert data["degradation_causes"]["timeout"] == 1
    assert data["degradation_causes"]["cancelled"] == 1
    assert data["degradation_causes"]["execution_error"] == 0


def test_missing_measurements_are_explicitly_unavailable():
    from src.web.api.copilot import build_copilot_run_baseline

    data = build_copilot_run_baseline([_row()], days=7)
    cov = data["coverage"]

    assert cov["ttft"]["available"] is False
    assert "TTFT" in cov["ttft"]["reason"] or "首字" in cov["ttft"]["reason"]
    assert cov["queue_wait"]["available"] is False
    assert cov["stage_timings"]["available"] is False
    # 没有 ttft 字段就不许在 latency 里出现假的 ttft
    assert "ttft_ms" not in data["latency"]


def test_days_window_is_7_or_30_only():
    from src.web.api.copilot import build_copilot_run_baseline

    with pytest.raises(ValueError):
        build_copilot_run_baseline([], days=14)


def test_coverage_counts_steps_and_dispatch_reason():
    from src.web.api.copilot import build_copilot_run_baseline

    r1 = _row(dispatch_reason="recipe:deep_diagnosis", conversation_id=3)
    r2 = _row(dispatch_reason=None, conversation_id=None)
    steps = {r1.run_id: _steps(10)}
    data = build_copilot_run_baseline([r1, r2], days=7, steps_by_run=steps)
    cov = data["coverage"]

    assert cov["steps_present"] == {"n": 1, "of": 2, "ratio": 0.5}
    assert cov["dispatch_reason_present"] == {"n": 1, "of": 2, "ratio": 0.5}
    assert cov["conversation_linked"] == {"n": 1, "of": 2, "ratio": 0.5}


# ── 端点: 只读 + 只算本人 + 不补窗口外历史 ────────────────────────────────
@pytest.fixture()
def db(tmp_path):
    # TestClient 在另一个线程跑端点, sqlite 连接必须允许跨线程复用本 fixture 的会话
    eng = create_engine(
        f"sqlite:///{tmp_path/'baseline.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=eng, tables=[CopilotRun.__table__, CopilotStep.__table__])
    Sess = sessionmaker(bind=eng)
    s = Sess()
    try:
        yield s
    finally:
        s.close()


class _User:
    def __init__(self, uid):
        self.id = uid
        self.role = "member"


def _add(db, uid, *, days_ago=0, recipe="deep_diagnosis", status="ok",
         elapsed_ms=1000, error=None):
    from src.core.timezone import db_now_naive

    rid = str(uuid.uuid4())
    db.add(CopilotRun(
        run_id=rid, user_id=uid, recipe=recipe, status=status,
        question="q", tool_calls=1, elapsed_ms=elapsed_ms, error=error,
        # 与端点同一时钟：db_now_naive 是本地朴素时间, 用 utcnow 会白送 8 小时误差
        created_at=db_now_naive() - timedelta(days=days_ago),
    ))
    db.commit()
    return rid


def test_endpoint_only_counts_own_rows_in_window(db):
    from src.web.api.copilot import get_copilot_run_baseline

    me, other = str(uuid.uuid4()), str(uuid.uuid4())
    _add(db, me, days_ago=1)
    _add(db, me, days_ago=8)          # 7 天窗口外
    _add(db, me, days_ago=29)
    _add(db, me, days_ago=31)         # 30 天窗口外
    _add(db, other, days_ago=1)       # 别人的

    d7 = get_copilot_run_baseline(days=7, db=db, user=_User(me))
    d30 = get_copilot_run_baseline(days=30, db=db, user=_User(me))

    assert d7["runs"] == 1
    # 30 天窗口含 1/8/29 天前三条; 31 天前与别人的行都不算
    assert d30["runs"] == 3
    assert d7["days"] == 7 and d30["days"] == 30


def test_endpoint_rejects_other_windows(db):
    from fastapi import HTTPException

    from src.web.api.copilot import get_copilot_run_baseline

    with pytest.raises(HTTPException) as ei:
        get_copilot_run_baseline(days=14, db=db, user=_User("u"))
    assert ei.value.status_code == 422


def test_endpoint_groups_steps_by_run(db):
    from src.web.api.copilot import get_copilot_run_baseline

    me = str(uuid.uuid4())
    rid = _add(db, me)
    db.add(CopilotStep(run_id=rid, seq=1, tool="get_kline", ok=True, elapsed_ms=250))
    db.add(CopilotStep(run_id=rid, seq=2, tool="get_quote", ok=True, elapsed_ms=150))
    db.commit()

    data = get_copilot_run_baseline(days=7, db=db, user=_User(me))
    assert data["tools"]["n_steps"] == 2
    assert data["tools"]["sum_ms"] == 400


def test_baseline_route_is_wired_and_rejects_bad_window(db):
    """HTTP 层钉住路由真的挂上了 /api/copilot/baseline（不是只有纯函数能跑）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.web.api import copilot as copilot_api
    from src.web.api.auth import get_current_user

    me = str(uuid.uuid4())
    _add(db, me)

    app = FastAPI()
    app.include_router(copilot_api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _User(me)
    app.dependency_overrides[copilot_api.get_db] = lambda: db

    with TestClient(app) as c:
        r = c.get("/api/copilot/baseline?days=7")
        assert r.status_code == 200
        assert r.json()["days"] == 7
        assert r.json()["runs"] == 1
        assert c.get("/api/copilot/baseline?days=14").status_code == 422


# ── 阶段打点: 低敏、可观测、不落问题与参数 ────────────────────────────────
def test_trace_stage_marks_are_measured_and_low_sensitivity(monkeypatch, caplog):
    from src.harness import trace as trace_mod

    trace = trace_mod.CopilotTrace(question="看看我持仓 600111 的筹码")
    with caplog.at_level(logging.INFO, logger=trace_mod.logger.name):
        trace.mark_stage("first_answer_byte")
        trace.mark_stage("final", ok=False, detail="degraded")

    assert [s["stage"] for s in trace.stages] == ["first_answer_byte", "final"]
    assert all(set(s) == {"stage", "ms", "ok"} for s in trace.stages)
    assert all(isinstance(s["ms"], int) and s["ms"] >= 0 for s in trace.stages)
    assert trace.stages[-1]["ok"] is False

    lines = [r.getMessage() for r in caplog.records if "copilot.stage" in r.getMessage()]
    assert len(lines) == 2
    payload = json.loads(lines[0].split("] ", 1)[1])
    assert payload["run_id"] == trace.run_id
    assert payload["stage"] == "first_answer_byte"
    assert "600111" not in lines[0] and "持仓" not in lines[0]


def test_trace_stage_mark_never_raises_on_bad_input():
    from src.harness.trace import CopilotTrace

    trace = CopilotTrace()
    trace.mark_stage("x" * 500)          # 超长名应被截断而不是抛错
    assert len(trace.stages[0]["stage"]) <= 32
