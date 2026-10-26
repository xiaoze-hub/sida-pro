"""状态快照、匿名裁剪与四账号通知隔离；不调用真实外部服务。"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web.api import health, system_status
from src.web.api.auth import get_current_user
from src.web.database import Base, get_db
from src.web.models import NotifyChannel


def _payload():
    return {"version": "test", "status": "ok", "components": {
        "database": {"status": "ok", "latency_ms": 4, "url": "secret-db-url", "dialect": "postgresql"},
        "redis": {"status": "ok", "url": "secret-redis-url"},
        "biz_cache": {"l1_entries": 3, "redis": "ok"},
        "scheduler": {"status": "ok", "leader_state": "standby", "running": 0},
        "forecast_engine": {"status": "down", "error": "private-error", "url": "private-host"}}}


def test_snapshot_preserves_unknown_and_does_not_expose_secrets():
    result = system_status.build_status_snapshot(_payload(), [{"id": 7, "name": "源", "status": "connected"}], 2, 1)
    nodes = {item["id"]: item for item in result["nodes"]}
    assert nodes["datasource"]["status"] == "unknown"
    assert not nodes["datasource"]["connected"]
    assert nodes["ai"]["status"] == "unknown"
    assert nodes["scheduler"]["status"] == "standby"
    assert not nodes["scheduler"]["connected"]
    assert nodes["database"]["latency_ms"] == 4
    assert nodes["forecast_engine"]["status"] == "fail"
    assert all(secret not in str(result) for secret in ("secret-db-url", "secret-redis-url", "private-error", "private-host"))
    assert {edge["id"] for edge in result["edges"] if edge["connected"]} == {"api-database", "api-biz_cache", "biz_cache-redis"}


def test_missing_components_remain_unknown():
    result = system_status.build_status_snapshot({}, [], 0, 0)
    assert next(node for node in result["nodes"] if node["id"] == "database")["status"] == "unknown"
    assert not any(edge["connected"] for edge in result["edges"])


@pytest.fixture
def client_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    app = FastAPI()
    app.include_router(system_status.router, prefix="/api/health")
    app.dependency_overrides[get_db] = lambda: db
    current = SimpleNamespace(id="owner", role="owner")
    app.dependency_overrides[get_current_user] = lambda: current

    async def fake_components(on_component=None):
        if on_component:
            for key in ("database", "redis", "biz_cache", "scheduler", "forecast_engine"):
                on_component(key, _payload()["components"][key])
        return _payload()

    monkeypatch.setattr(health, "_check_components", fake_components)
    with TestClient(app) as client:
        yield client, db, current
    db.close()
    engine.dispose()


def test_read_only_snapshot_and_list_do_not_probe_ai_or_send(client_db, monkeypatch):
    from src.core import selfcheck
    client, _db, _current = client_db

    async def forbidden(*args, **kwargs):
        raise AssertionError("只读页面禁止调用 AI 或发送通知")

    monkeypatch.setattr(selfcheck, "probe_ai_model", forbidden)
    monkeypatch.setattr(selfcheck, "probe_notify_channel", forbidden)
    assert client.get("/api/health/system").status_code == 200
    response = client.get("/api/health/selfcheck?list=1")
    assert response.status_code == 200
    assert len(response.json()["items"]) >= 3
    assert client.get("/api/health/selfcheck").status_code == 405


@pytest.mark.parametrize("role", ["guest", "member", "pro"])
def test_operational_endpoints_require_owner(client_db, role):
    client, _db, current = client_db
    current.role = role
    assert client.get("/api/health/system").status_code == 403
    assert client.get("/api/health/system/stream").status_code == 403
    assert client.get("/api/health/selfcheck?list=1").status_code == 403
    assert client.post("/api/health/selfcheck", json={"keys": ["sys:db"]}).status_code == 403


def test_four_account_notification_scope_and_post_defaults(client_db, monkeypatch):
    from src.core import selfcheck
    client, db, current = client_db
    channels = [NotifyChannel(name=name, type="webhook", user_id=user_id, enabled=True) for name, user_id in
                (("a", "owner"), ("b", "two"), ("c", "three"), ("d", "four"), ("shared", None))]
    db.add_all(channels)
    db.commit()
    sends = []

    async def fake_notify(channel, *, send=False):
        sends.append((channel.name, send))
        return selfcheck._item("notify", f"nc:{channel.id}", channel.name, "ok", 1, note="仅校验配置，未真实发送")

    monkeypatch.setattr(selfcheck, "probe_notify_channel", fake_notify)
    for user_id in ("owner", "two", "three", "four"):
        current.id = user_id
        items = client.get("/api/health/selfcheck?list=1").json()["items"]
        visible = {item["key"] for item in items if item["category"] == "notify"}
        expected = {f"nc:{channel.id}" for channel in channels if channel.user_id in (user_id, None)}
        assert visible == expected
        response = client.post("/api/health/selfcheck", json={"keys": [f"nc:{ch.id}" for ch in channels]})
        assert {item["key"] for item in response.json()["items"]} == expected
    assert sends and not any(send for _name, send in sends)
    assert client.post("/api/health/selfcheck", json={"keys": []}).status_code == 400


def test_public_health_still_strips_external_details(monkeypatch):
    async def fake_components(on_component=None):
        if on_component:
            for key in ("database", "redis", "biz_cache", "scheduler", "forecast_engine"):
                on_component(key, _payload()["components"][key])
        return _payload()

    monkeypatch.setattr(health, "_check_components", fake_components)
    request = SimpleNamespace(client=SimpleNamespace(host="8.8.8.8"))
    assert asyncio.run(health.health(request)) == {"status": "ok", "version": "test"}


def test_stream_publishes_real_module_progress_without_private_fields(client_db):
    import json
    client, _db, _current = client_db
    response = client.get("/api/health/system/stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[0]["done_count"] == 4
    assert [event["done_count"] for event in events[1:-1]] == [5, 6, 7, 8, 9]
    assert events[-1]["done"] is True
    assert events[-1]["version"] == "test"
    ids = {node["id"] for event in events for node in event["nodes"]}
    assert len(ids) == 9
    assert all(secret not in response.text for secret in ("secret-db-url", "secret-redis-url", "private-error", "private-host"))
