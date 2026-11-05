"""定时任务投递必须遵守账号边界，且失败不能记为成功。"""
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core import copilot_schedule as schedule
from src.db.models import Base, ChatConversation, ChatMessage, CopilotSchedule, CopilotScheduleRun
import src.db.session as db_session
from fastapi import HTTPException
from src.web.api.copilot import get_schedule_runs, post_schedule


@pytest.fixture
def sessions(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", factory)
    monkeypatch.setattr(schedule, "is_trading_day", lambda _: True)
    yield factory
    engine.dispose()


def test_delivery_never_writes_to_other_users_conversation(sessions):
    db = sessions()
    other = ChatConversation(user_id="other", title="私有会话")
    db.add(other)
    db.commit()
    with pytest.raises(ValueError, match="无权访问"):
        schedule.create(db, "owner", kind="daily_brief", at_hhmm="15:00", conversation_id=other.id)
    row = CopilotSchedule(user_id="owner", kind="daily_brief", at_hhmm="15:00", conversation_id=other.id)
    db.add(row)
    db.flush()
    schedule._deliver(db, row, "摘要")
    db.commit()
    assert row.conversation_id != other.id
    assert db.query(ChatConversation).filter_by(id=row.conversation_id).one().user_id == "owner"
    assert db.query(ChatMessage).filter_by(conversation_id=other.id).count() == 0
    assert db.query(ChatMessage).filter_by(conversation_id=row.conversation_id).count() == 1
    db.close()


def test_delivery_failure_is_recorded_and_next_schedule_runs(sessions, monkeypatch):
    db = sessions()
    first = schedule.create(db, "a", kind="daily_brief", at_hhmm="15:00")
    second = schedule.create(db, "b", kind="daily_brief", at_hhmm="15:00")
    monkeypatch.setattr(schedule, "_build_text", lambda row: "摘要")
    deliver = schedule._deliver

    def fail_first(db, row, text):
        if row.id == first["id"]:
            raise RuntimeError("delivery failed")
        deliver(db, row, text)

    monkeypatch.setattr(schedule, "_deliver", fail_first)
    result = schedule.run_due(datetime(2026, 11, 3, 15, 0))
    assert result["failed"] == 1
    assert result["ran"] == 1
    db.expire_all()
    assert db.query(CopilotScheduleRun).filter_by(schedule_id=first["id"]).one().status == "failed"
    assert db.query(CopilotScheduleRun).filter_by(schedule_id=second["id"]).one().status == "ok"
    assert db.query(CopilotSchedule).filter_by(id=first["id"]).one().last_run_date is None
    db.close()


@pytest.mark.parametrize("hour,minute", [(11, 30), (12, 0), (15, 0)])
def test_intraday_excludes_break_and_close(sessions, hour, minute):
    row = CopilotSchedule(user_id="a", kind="daily_brief", at_hhmm="09:30", weekdays="mon-fri", window_kind="intraday")
    assert not schedule._should_trigger_now(row, datetime(2026, 11, 3, hour, minute))


def test_schedule_api_preserves_window_and_scopes_records(sessions):
    db = sessions()
    owner = SimpleNamespace(id="owner")
    row = post_schedule({"kind": "daily_brief", "at_hhmm": "09:30", "window_kind": "intraday", "interval_min": 15}, db, owner)
    assert row["window_kind"] == "intraday"
    assert row["interval_min"] == 15
    db.add(CopilotScheduleRun(schedule_id=row["id"], user_id="owner", status="failed", error="secret trace_id"))
    db.commit()
    assert get_schedule_runs(row["id"], db, owner)[0]["error"] == "任务执行失败，请稍后重试"
    with pytest.raises(HTTPException) as exc:
        get_schedule_runs(row["id"], db, SimpleNamespace(id="other"))
    assert exc.value.status_code == 404
    db.close()


def test_intraday_failure_observes_interval(sessions):
    db = sessions()
    row = schedule.create(db, "a", kind="daily_brief", at_hhmm="09:30", window_kind="intraday", interval_min=20)
    db.add(CopilotScheduleRun(schedule_id=row["id"], user_id="a", status="failed", created_at=datetime(2026, 11, 3, 9, 30)))
    db.commit()
    task = db.query(CopilotSchedule).filter_by(id=row["id"]).one()
    assert not schedule._should_trigger_now(task, datetime(2026, 11, 3, 9, 35))
    assert schedule._should_trigger_now(task, datetime(2026, 11, 3, 9, 50))
    db.close()
