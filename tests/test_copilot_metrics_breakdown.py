"""治理看板「按配方质量」拆解（P3，2026-10-22）。

背景：`/copilot/metrics` 的 `by_recipe` 原来只给条数 —— 一份配方跑 10 次全 degraded
和跑 10 次全 ok 在旧口径里长得一样，治理看板看不出「哪份配方在坏」。

铁律：
- 旧字段（recipe / runs）必须保留 → 老前端不炸；
- 算不出的均值写 None，不写 0（0ms 和「没测到」是两件事）；
- 没有运行记录时 `degraded_rate` 是 None（分母不存在），`zero_tool_runs` 是 0。
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, CopilotRun


@pytest.fixture()
def db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'metrics.db'}")
    Base.metadata.create_all(bind=eng, tables=[CopilotRun.__table__])
    Sess = sessionmaker(bind=eng)
    s = Sess()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture()
def uid():
    return str(uuid.uuid4())


class _User:
    def __init__(self, uid: str):
        self.id = uid
        self.role = "member"


def _run(db, uid, *, recipe=None, status="ok", tool_calls=2, elapsed_ms=100):
    db.add(CopilotRun(
        run_id=str(uuid.uuid4()), user_id=uid, status=status, recipe=recipe,
        tool_calls=tool_calls, elapsed_ms=elapsed_ms, created_at=datetime.utcnow(),
    ))
    db.commit()


def _metrics(db, uid, days=7):
    from src.web.api.copilot import get_copilot_metrics

    return get_copilot_metrics(days=days, db=db, user=_User(uid))


def test_empty_returns_none_rate_not_zero(db, uid):
    data = _metrics(db, uid)
    assert data["runs"] == 0
    assert data["by_recipe"] == []
    # 分母不存在 → None，不写 0（0% 降级和「没跑过」必须区分）
    assert data["degraded_rate"] is None
    assert data["zero_tool_runs"] == 0


def test_by_recipe_breaks_down_status_and_latency(db, uid):
    _run(db, uid, recipe="deep_diagnosis", status="ok", tool_calls=5, elapsed_ms=1000)
    _run(db, uid, recipe="deep_diagnosis", status="degraded", tool_calls=2, elapsed_ms=3000)
    _run(db, uid, recipe="quick_quote", status="error", tool_calls=0, elapsed_ms=500)
    _run(db, uid, recipe="quick_quote", status="ok", tool_calls=1, elapsed_ms=500)

    data = _metrics(db, uid)
    by = {r["recipe"]: r for r in data["by_recipe"]}

    assert set(by) == {"deep_diagnosis", "quick_quote"}
    # 旧字段保留
    assert by["deep_diagnosis"]["runs"] == 2
    # 新字段
    assert by["deep_diagnosis"]["ok_rate"] == 0.5
    assert by["deep_diagnosis"]["degraded_runs"] == 1
    assert by["deep_diagnosis"]["error_runs"] == 0
    assert by["deep_diagnosis"]["zero_tool_runs"] == 0
    assert by["deep_diagnosis"]["avg_elapsed_ms"] == 2000

    assert by["quick_quote"]["ok_rate"] == 0.5
    assert by["quick_quote"]["error_runs"] == 1
    assert by["quick_quote"]["zero_tool_runs"] == 1
    assert by["quick_quote"]["avg_elapsed_ms"] == 500

    assert data["degraded_rate"] == 0.25
    assert data["zero_tool_runs"] == 1


def test_missing_recipe_grouped_as_default(db, uid):
    """没有配方的运行（问价/闲聊）单独一组，不许混进某个具名配方。"""
    _run(db, uid, recipe=None, status="ok")
    data = _metrics(db, uid)
    assert data["by_recipe"][0]["recipe"] == "(默认)"


def test_avg_elapsed_none_when_not_measured(db, uid):
    """耗时没记录 → None，不是 0。"""
    _run(db, uid, recipe="deep_diagnosis", status="ok", elapsed_ms=None)
    item = _metrics(db, uid)["by_recipe"][0]
    assert item["avg_elapsed_ms"] is None


def test_by_recipe_sorted_by_runs_desc(db, uid):
    _run(db, uid, recipe="a", status="ok")
    for _ in range(3):
        _run(db, uid, recipe="b", status="ok")
    assert [r["recipe"] for r in _metrics(db, uid)["by_recipe"]] == ["b", "a"]


def test_only_own_runs_counted(db, uid):
    other = str(uuid.uuid4())
    _run(db, uid, recipe="deep_diagnosis", status="ok")
    _run(db, other, recipe="deep_diagnosis", status="error")
    data = _metrics(db, uid)
    assert data["runs"] == 1
    assert data["by_recipe"][0]["error_runs"] == 0
