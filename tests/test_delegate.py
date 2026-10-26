"""委派子 run（P1-1.1）契约测试。

钉住四件事（都是曾经踩过或明确禁止的）：
  1. 子 run 必须有 `parent_run_id`，否则父 run 无法归因；
  2. **子 run 预算独立** —— 子 run 的工具调用不计入父 run；
  3. 子 run 失败**不自动重跑**，如实落 failed 并在汇总里列为缺口；
  4. 不新增 chat 工具（委派是内部能力，注册表 77 是钉子）。

全部用 stub executor，不打 LLM、不相对于配额。
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import delegate as deleg
from src.db.models import Base, CopilotRun


@pytest.fixture()
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    CopilotRun.__table__.create(eng, checkfirst=True)
    Session = sessionmaker(bind=eng)
    s = Session()
    # 父 run（普通 run，无 parent）
    s.add(CopilotRun(run_id="parent-1", user_id="u1", recipe="deep_diagnosis",
                     question="帮我看看这几只票", status="running", tool_calls=4))
    s.commit()
    yield s
    s.close()


def _ok_executor(**kw):
    return {"tool_calls": 3, "elapsed_ms": 1200}


def test_子run带parent_run_id(db):
    out = deleg.delegate(db, parent_run_id="parent-1", user_id="u1",
                         recipe_id="chip_analyst", question="看筹码", executor=_ok_executor)
    row = db.query(CopilotRun).filter(CopilotRun.run_id == out["run_id"]).first()
    assert row is not None
    assert row.parent_run_id == "parent-1"
    assert row.recipe == "chip_analyst"
    assert row.status == "ok"


def test_子run预算独立_不扣父run(db):
    before = db.query(CopilotRun).filter(CopilotRun.run_id == "parent-1").first().tool_calls
    deleg.delegate(db, parent_run_id="parent-1", user_id="u1",
                   recipe_id="chip_analyst", question="看筹码", executor=_ok_executor)
    after = db.query(CopilotRun).filter(CopilotRun.run_id == "parent-1").first().tool_calls
    assert after == before, "子 run 的工具调用不能并进父 run（预算必须独立）"

    agg = deleg.aggregate(db, "parent-1")
    assert agg["tool_calls"] == 3, "汇总里的工具调用是子 run 自己的累计"


def test_子run失败不自动重跑_并列入缺口(db):
    calls = {"n": 0}

    def boom(**kw):
        calls["n"] += 1
        raise RuntimeError("上游超时")

    out = deleg.delegate(db, parent_run_id="parent-1", user_id="u1",
                         recipe_id="chip_analyst", question="看筹码", executor=boom)
    assert out["status"] == "failed"
    assert calls["n"] == 1, f"失败只能执行一次，实际 {calls['n']} 次（不许自动重跑）"

    agg = deleg.aggregate(db, "parent-1")
    assert agg["failed"] == 1
    assert any("上游超时" in g for g in agg["gaps"]), "缺口必须写明原因"


def test_没有执行体不假装成功(db):
    out = deleg.delegate(db, parent_run_id="parent-1", user_id="u1",
                         recipe_id="chip_analyst", question="看筹码", executor=None)
    assert out["status"] == "failed"
    assert "没有可用的执行体" in out["error"]


def test_汇总_没有委派时说清楚(db):
    agg = deleg.aggregate(db, "parent-1")
    assert agg["available"] is False
    assert "没有委派" in agg["reason"]
    assert agg["children"] == []


def test_多个子run按recipe归集(db):
    out = deleg.delegate_many(
        db, parent_run_id="parent-1", user_id="u1",
        tasks=[("chip_analyst", "看筹码"), ("theme_leader", "谁是龙头")],
        executor=_ok_executor,
    )
    assert out["summary"]["count"] == 2
    assert {c["recipe_id"] for c in out["summary"]["children"]} == {"chip_analyst", "theme_leader"}
    assert out["summary"]["ok"] == 2
    assert out["summary"]["gaps"] == []


def test_子run默认预算比父小():
    assert 1 <= deleg.DEFAULT_CHILD_BUDGET <= 8, "子任务范围窄，预算不该给得比父 run 还大"


def test_委派不新增chat工具():
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    assert len(CHAT_TOOL_REGISTRY) == 78, "委派是内部能力，不许变成模型可调工具"
    assert "delegate" not in CHAT_TOOL_REGISTRY
