"""多用户配额与成本看板（P3 治理）单测 —— 2026-10-20。

铁律：
- 没有配额行 = 不限（老用户不受影响）
- 超限是**硬拒**（返回 429），不是提示词
- 统计不到就是 None，绝不补 0 冒充"已用完"
- 没配单价的模型 cost=None，不编造金额
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core import copilot_quota as q
from src.db.models import Base, CopilotQuota, CopilotRun, LLMUsage


@pytest.fixture()
def db(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path/'q.db'}")
    Base.metadata.create_all(bind=eng, tables=[
        CopilotQuota.__table__, CopilotRun.__table__, LLMUsage.__table__,
    ])
    Sess = sessionmaker(bind=eng)
    s = Sess()
    yield s
    s.close()


@pytest.fixture()
def user_id():
    return str(uuid.uuid4())


def _run(db, uid, *, tokens=None, status="ok"):
    rid = str(uuid.uuid4())
    db.add(CopilotRun(run_id=rid, user_id=uid, status=status, tool_calls=1,
                      elapsed_ms=100, created_at=datetime.utcnow()))
    if tokens:
        db.add(LLMUsage(scene="chat", model_name="deepseek-v4-pro", prompt_tokens=tokens,
                        completion_tokens=0, user_id=uid, run_id=rid,
                        created_at=datetime.utcnow()))
    db.commit()
    return rid


def test_no_quota_row_means_unlimited(db, user_id):
    ok, reason, usage = q.check(db, user_id)
    assert ok is True and reason is None
    assert usage["runs"] == 0 and usage["tokens"] == 0


def test_daily_runs_limit_blocks(db, user_id):
    q.set_quota(db, user_id, daily_runs=2, daily_tokens=None)
    _run(db, user_id)
    assert q.check(db, user_id)[0] is True
    _run(db, user_id)
    ok, reason, usage = q.check(db, user_id)
    assert ok is False
    assert "2 轮/天" in reason and "00:00" in reason
    assert usage["runs"] == 2


def test_daily_token_limit_blocks(db, user_id):
    q.set_quota(db, user_id, daily_runs=None, daily_tokens=1000)
    _run(db, user_id, tokens=1200)
    ok, reason, usage = q.check(db, user_id)
    assert ok is False
    assert "tokens" in reason
    assert usage["tokens"] == 1200


def test_disabled_quota_row_does_not_block(db, user_id):
    q.set_quota(db, user_id, daily_runs=1, enabled=False)
    _run(db, user_id)
    _run(db, user_id)
    assert q.check(db, user_id)[0] is True


def test_set_quota_upserts(db, user_id):
    q.set_quota(db, user_id, daily_runs=5)
    q.set_quota(db, user_id, daily_runs=9, daily_tokens=500)
    rows = db.query(CopilotQuota).filter(CopilotQuota.user_id == user_id).all()
    assert len(rows) == 1
    assert rows[0].daily_runs == 9 and rows[0].daily_tokens == 500


def test_enforce_raises_429(db, user_id):
    from fastapi import HTTPException

    q.set_quota(db, user_id, daily_runs=1)
    _run(db, user_id)

    class _U:
        id = user_id

    with pytest.raises(HTTPException) as ei:
        q.enforce(db, _U())
    assert ei.value.status_code == 429
    assert ei.value.detail["code"] == "copilot_quota_exceeded"


def test_cost_summary_prices_known_model_and_flags_unknown(db, user_id):
    _run(db, user_id, tokens=2_000_000)
    db.add(LLMUsage(scene="chat", model_name="mystery-model", prompt_tokens=500_000,
                    completion_tokens=0, user_id=user_id, created_at=datetime.utcnow()))
    db.commit()

    data = q.cost_summary(db, days=7, price_per_mtoken={"deepseek": 2.0})
    total = data["total_tokens"]
    assert total == 2_500_000
    assert data["unpriced_models"] == ["mystery-model"]
    by_model = {r["model"]: r for r in data["by_model"]}
    assert by_model["deepseek-v4-pro"]["cost"] == pytest.approx(4.0)
    assert by_model["mystery-model"]["cost"] is None, "没配单价不许编金额"
