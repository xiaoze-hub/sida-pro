# -*- coding: utf-8 -*-
"""邮箱注册 + API Key 管理控制台冒烟(2026-09-16)。"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


def _data(resp) -> dict:
    """解开 {code,success,data,message} 响应壳。"""
    body = resp.json()
    if isinstance(body, dict) and "data" in body and "success" in body:
        return body.get("data") or {}
    return body if isinstance(body, dict) else {}


@pytest.fixture(scope="module")
def client():
    os.environ.setdefault("SKILL_KEY_SALT", "test-salt-email-reg")
    from src.web.app import app

    with TestClient(app) as c:
        yield c


def _register(client, email, password="password123", username=None):
    # 先发验证码(开发模式: SMTP 未配置时验证码打印到日志)
    from src.web.api.email_verify import _store, _lock, _store_key, EmailCode
    import secrets, time as _time
    code = f"{secrets.randbelow(1000000):06d}"
    email_n = email.lower().strip()
    with _lock:
        _store[_store_key(email_n, "register")] = EmailCode(
            code=code, email=email_n, purpose="register",
            created_at=_time.time(), used=False,
        )
    payload = {"email": email, "password": password, "code": code}
    if username is not None:
        payload["username"] = username
    return client.post("/api/auth/register", json=payload)


def _login(client, ident, password="password123"):
    r = client.post("/api/auth/login", json={"username": ident, "password": password})
    assert r.status_code == 200, r.text
    return _data(r)


def test_email_register_and_login(client):
    r = _register(client, "Alice.Doe@Example.com")
    assert r.status_code == 200, r.text
    body = _data(r)
    assert body["email"] == "alice.doe@example.com"
    # email 先 normalize 成小写, 前缀去非字母数字 → alicedoe
    assert body["username"] == "alicedoe"
    assert body["api_key"] and body["api_key"].startswith("sk_")

    # 邮箱重复
    r2 = _register(client, "alice.doe@example.com", username="otheruser")
    assert r2.status_code == 400

    # 非法邮箱
    r3 = _register(client, "not-an-email")
    assert r3.status_code == 400

    # 显式用户名
    r4 = _register(client, "bob@example.com", username="bobuser")
    assert r4.status_code == 200
    assert _data(r4)["username"] == "bobuser"

    # email 登录
    d5 = _login(client, "alice.doe@example.com")
    assert d5["user"]["email"] == "alice.doe@example.com"

    # username 登录
    _login(client, "alicedoe")


def test_api_key_management_console(client):
    # 专用账号: 注册时拿到自动 key 明文, 用于验证 reset 后旧 key 立即失效
    email = "console@example.com"
    reg = _data(_register(client, email, username="consoleu"))
    old_raw = reg["api_key"]
    assert old_raw.startswith("sk_")

    d = _login(client, email)
    tok = d["token"]
    ha = {"Authorization": f"Bearer {tok}"}

    # 列表
    r = client.get("/api/keys", headers=ha)
    assert r.status_code == 200, r.text
    keys = _data(r)["keys"]
    assert len(keys) >= 1
    k0 = keys[0]
    for field in ("id", "key_prefix", "tier", "status", "created_at"):
        assert field in k0
    assert "api_key" not in k0 and "key_hash" not in k0
    target_id = k0["id"]

    # 旧 key 当前可用
    assert client.get("/api/skills", headers={"X-API-Key": old_raw}).status_code == 200

    # 创建(线上 POST /api/keys 由 skills_gateway 优先匹配, 登录态同样返回明文)
    r2 = client.post("/api/keys", headers=ha, json={})
    assert r2.status_code == 200, r2.text
    assert _data(r2)["api_key"].startswith("sk_")

    # 用量
    r3 = client.get(f"/api/keys/{target_id}/usage", headers=ha)
    assert r3.status_code == 200, r3.text
    u3 = _data(r3)
    assert "used_today" in u3
    assert "remaining_today" in u3

    # 重置: 新明文可用, 旧明文立即失效
    r4 = client.post(f"/api/keys/{target_id}/reset", headers=ha)
    assert r4.status_code == 200, r4.text
    new_raw = _data(r4)["api_key"]
    assert new_raw.startswith("sk_") and new_raw != old_raw
    assert client.get("/api/skills", headers={"X-API-Key": new_raw}).status_code == 200
    old_resp = client.get("/api/skills", headers={"X-API-Key": old_raw})
    assert old_resp.status_code in (401, 403), old_resp.text

    # 隔离
    _register(client, "other2@example.com", username="other2u")
    d2 = _login(client, "other2@example.com")
    hb = {"Authorization": f"Bearer {d2['token']}"}
    assert client.get(f"/api/keys/{target_id}/usage", headers=hb).status_code == 404
    assert client.delete(f"/api/keys/{target_id}", headers=hb).status_code == 404

    # 软删
    r5 = client.delete(f"/api/keys/{target_id}", headers=ha)
    assert r5.status_code == 200, r5.text
    assert _data(r5)["status"] == "disabled"
    still = [k for k in _data(client.get("/api/keys", headers=ha))["keys"] if k["id"] == target_id]
    assert len(still) == 1 and still[0]["status"] == "disabled"


def test_migration_172_email_column_and_unique_index(client):
    from sqlalchemy import inspect as sa_inspect

    from src.db.session import engine

    insp = sa_inspect(engine)
    cols = {c["name"] for c in insp.get_columns("users")}
    assert "email" in cols
    idx = insp.get_indexes("users")
    email_idx = [
        i
        for i in idx
        if "email" in (i.get("name") or "") or "email" in i.get("column_names", [])
    ]
    assert email_idx, f"email index missing: {idx}"
    assert any(i.get("unique") for i in email_idx)
