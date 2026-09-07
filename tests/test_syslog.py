"""系统日志回归: scheduler 监听 + JSONL 轮转 + errors/上报接口。"""
from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M  # noqa: F401
from src.web.api import logs as logs_api
from src.web.api.auth import get_current_user
from src.web.database import Base, get_db
from src.core import error_tracker as et


class _StubScheduler:
    def __init__(self):
        self.listeners = []

    def add_listener(self, cb, mask):
        self.listeners.append((cb, mask))


class _FakeEvent:
    def __init__(self, job_id="cron_x", exc=None):
        self.job_id = job_id
        self.exception = exc


class _Owner:
    id = "owner-1"
    username = "admin"
    role = "owner"


def _client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("ERROR_TRACKER_FILE", str(tmp_path / "err.jsonl"))
    et._clear_state()
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    app = FastAPI()
    app.include_router(logs_api.router, prefix="/logs")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db

    async def _owner():
        return _Owner()

    app.dependency_overrides[get_current_user] = _owner
    return TestClient(app, raise_server_exceptions=False)


def test_scheduler_listener_catches_job_error(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    sched = _StubScheduler()
    assert et.install_scheduler_error_tracking(sched) is True
    assert et.install_scheduler_error_tracking(None) is False
    assert et.install_scheduler_error_tracking(object()) is False
    cb, _ = sched.listeners[0]
    cb(_FakeEvent(exc=RuntimeError("boom-job")))
    r = c.get("/logs/errors")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert any("boom-job" in (i.get("message") or "") for i in items), items
    assert any((i.get("context") or {}).get("source") == "scheduler" for i in items)


def test_scheduler_missed_job_reported(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    sched = _StubScheduler()
    et.install_scheduler_error_tracking(sched)
    cb, _ = sched.listeners[0]
    cb(_FakeEvent(job_id="nightly", exc=None))
    assert et.recent_errors(10), "missed job 应落盘"


def test_frontend_report_endpoint(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    r = c.post(
        "/logs/frontend",
        json={"type": "TypeError", "message": "x is null", "stack": "at foo", "url": "/quote"},
    )
    assert r.status_code == 200, r.text
    items = et.recent_errors(10)
    assert any("[frontend]" in (i.get("message") or "") for i in items), items


def test_jsonl_rotation(monkeypatch, tmp_path):
    import json as _json

    monkeypatch.setenv("ERROR_TRACKER_FILE", str(tmp_path / "rot.jsonl"))
    et._clear_state()
    p = tmp_path / "rot.jsonl"
    p.write_text("".join(_json.dumps({"n": i}) + "\n" for i in range(2000)), encoding="utf-8")
    et._write_event({"n": "new"})
    lines = p.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 2001, len(lines)
    assert _json.loads(lines[-1]) == {"n": "new"}
    assert _json.loads(lines[0]) != {"n": 0}, "最老一半应被丢掉"
