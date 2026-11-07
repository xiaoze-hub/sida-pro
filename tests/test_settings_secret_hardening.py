# -*- coding: utf-8 -*-
"""设置接口复审补丁(2026-11-07 #2): 代理 userinfo 泄露 + 头像事务回滚缺陷。

真实 HTTP(TestClient 打真 app + 真 router + 真 DB), 每层都不打桩, 只把
`require_settings_write` 换成已鉴权 actor(权限门槛本身由
tests/test_settings_write_authz.py 用真中间件覆盖)。

验收线:
1. GET /settings 里 http_proxy 的 userinfo 密码掩码(管理员/成员同口径),
   非凭证代理地址原样(设置项仍可编辑)
2. PUT 回填掩码 userinfo → 用旧值补回真密码(不覆盖凭证), 主机可改
3. PUT 掩码且无处可还原 → 400, 掩码**绝不落库**
4. PUT 完整代理地址 → 落库明文, 响应体不含明文
5. 头像上传 commit 失败 → 503, DB 原值不变, 旧文件仍在(磁盘未被事务外改动)
6. 头像清空 commit 失败 → 503, 旧文件与 DB 都还是原样
7. 头像成功: 写唯一名文件, 旧文件清理, GET 读回新图
"""
from __future__ import annotations

import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M  # noqa: F401 (模型注册)
from src.web.api import settings as settings_api
from src.web.database import Base, get_db

PROXY_KEY = "http_proxy"
OLD_JPG = "data:image/jpeg;base64,AAAA"
NEW_PNG = "data:image/png;base64,BBBB"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # 端点会 lazy import server.apply_proxy_env 改进程级 env: 不隔离的话本用例
    # 写进去的代理会泄漏给后续用例(经 Settings() 成为"env 默认"), 互相污染。
    stub = types.ModuleType("server")
    stub.apply_proxy_env = lambda _v: None
    monkeypatch.setitem(sys.modules, "server", stub)
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(settings_api.router, prefix="/settings")

    def _db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[settings_api.require_settings_write] = lambda: (
        settings_api.SettingsActor(id="t1", username="tester", role="owner")
    )
    c = TestClient(app, raise_server_exceptions=False)
    return c, Session, tmp_path


def _set_db(Session, key: str, value: str) -> None:
    db = Session()
    try:
        row = db.query(M.AppSettings).filter(M.AppSettings.key == key).first()
        if row:
            row.value = value
        else:
            db.add(M.AppSettings(key=key, value=value, description=""))
        db.commit()
    finally:
        db.close()


def _db_value(Session, key: str) -> str:
    db = Session()
    try:
        row = db.query(M.AppSettings).filter(M.AppSettings.key == key).first()
        return "" if row is None else (row.value or "")
    finally:
        db.close()


def _list_by_key(c: TestClient) -> dict:
    r = c.get("/settings")
    assert r.status_code == 200, r.text
    data = r.json()
    items = data.get("data", data) if isinstance(data, dict) else data
    return {i["key"]: i for i in items}


# ── 1. 读: 代理密码掩码 ────────────────────────────────────────────────
def test_get_masks_proxy_password(env):
    c, Session, _ = env
    _set_db(Session, PROXY_KEY, "http://proxyuser:supersecret@10.0.0.9:3128")
    row = _list_by_key(c)[PROXY_KEY]
    assert "supersecret" not in str(row), row
    assert row["value"] == f"http://proxyuser:{settings_api.SECRET_MASK}@10.0.0.9:3128"
    # 账号与主机保留: 设置页仍可读可改
    assert "proxyuser" in row["value"] and "10.0.0.9:3128" in row["value"]


def test_get_leaves_credential_free_proxy_alone(env):
    c, Session, _ = env
    _set_db(Session, PROXY_KEY, "http://10.0.0.9:3128")
    assert _list_by_key(c)[PROXY_KEY]["value"] == "http://10.0.0.9:3128"


def test_get_masks_env_default_proxy_with_credentials(env, monkeypatch):
    """DB 无值时 GET 回落的 env 默认也可能带 userinfo, 同样不得泄露。"""
    c, Session, _ = env
    monkeypatch.setattr(
        settings_api, "_get_env_defaults",
        lambda: {"http_proxy": "http://envuser:envpass@proxy.local:8080"},
    )
    val = _list_by_key(c)[PROXY_KEY]["value"]
    assert "envpass" not in val
    assert val == f"http://envuser:{settings_api.SECRET_MASK}@proxy.local:8080"


# ── 2/3/4. 写: 掩码不覆盖凭证 ──────────────────────────────────────────
def test_put_masked_proxy_userinfo_keeps_stored_password(env):
    c, Session, _ = env
    _set_db(Session, PROXY_KEY, "http://proxyuser:supersecret@10.0.0.9:3128")
    masked = _list_by_key(c)[PROXY_KEY]["value"]  # 前端拿到什么就回填什么
    r = c.put(f"/settings/{PROXY_KEY}", json={"value": masked.replace("10.0.0.9", "10.0.0.10")})
    assert r.status_code == 200, r.text
    assert "supersecret" not in r.text, "响应体泄露代理密码"
    stored = _db_value(Session, PROXY_KEY)
    assert stored == "http://proxyuser:supersecret@10.0.0.10:3128", stored


def test_put_masked_proxy_without_stored_credential_rejected(env):
    c, Session, _ = env
    # 无旧值: 掩码无处还原 → 400, 掩码不得落库
    r = c.put(f"/settings/{PROXY_KEY}", json={"value": "http://u:********@10.0.0.9:3128"})
    assert r.status_code == 400, r.text
    assert _db_value(Session, PROXY_KEY) == ""

    # 旧值本身无 userinfo: 同样拒绝, 不制造假凭证
    _set_db(Session, PROXY_KEY, "http://10.0.0.9:3128")
    r = c.put(f"/settings/{PROXY_KEY}", json={"value": "http://u:********@10.0.0.9:3128"})
    assert r.status_code == 400, r.text
    assert _db_value(Session, PROXY_KEY) == "http://10.0.0.9:3128"


def test_put_masked_proxy_reuses_env_default_credentials(env, monkeypatch):
    """DB 为空时生效的是 env 代理: 掩码回填也要能从 env 还原真凭证。"""
    c, Session, _ = env
    monkeypatch.setattr(
        settings_api, "_get_env_defaults",
        lambda: {"http_proxy": "http://envuser:envpass@proxy.local:8080"},
    )
    r = c.put(f"/settings/{PROXY_KEY}", json={"value": "http://envuser:********@proxy.local:9090"})
    assert r.status_code == 200, r.text
    assert _db_value(Session, PROXY_KEY) == "http://envuser:envpass@proxy.local:9090"


def test_put_full_proxy_credentials_stored_and_masked_in_response(env):
    c, Session, _ = env
    r = c.put(f"/settings/{PROXY_KEY}", json={"value": " http://u:newpass@10.0.0.11:3128 "})
    assert r.status_code == 200, r.text
    assert _db_value(Session, PROXY_KEY) == "http://u:newpass@10.0.0.11:3128"
    body = r.json()
    body = body.get("data", body) if isinstance(body, dict) else body
    assert body["value"] == f"http://u:{settings_api.SECRET_MASK}@10.0.0.11:3128"


# ── 5/6/7. 头像: 文件系统不得先于 DB 事务生效 ──────────────────────────
def _fail_commit(monkeypatch) -> dict:
    """让下一次 commit 抛错; 返回开关, 置 off=False 即恢复(不用 undo, 免得
    连带撤掉 DATA_DIR 等 fixture 的环境)。"""
    state = {"on": True}

    def _patched(self, *a, **k):
        if state["on"]:
            raise RuntimeError("db commit failed")
        return _REAL_COMMIT(self, *a, **k)

    monkeypatch.setattr(OrmSession, "commit", _patched, raising=True)
    return state


_REAL_COMMIT = OrmSession.commit


def _avatar_files(tmp_path) -> list[str]:
    d = tmp_path / "avatars"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def test_avatar_upload_commit_failure_keeps_old_image(env, monkeypatch):
    c, Session, tmp_path = env
    old = c.put("/settings/avatar", json={"value": OLD_JPG}).json()["value"]
    assert _avatar_files(tmp_path) == [old]

    state = _fail_commit(monkeypatch)
    r = c.put("/settings/avatar", json={"value": NEW_PNG})
    state["on"] = False

    assert r.status_code == 503, r.text
    assert _db_value(Session, "ui_avatar") == old, "DB 回滚后仍指向旧头像"
    assert _avatar_files(tmp_path) == [old], "commit 失败却留下新文件/临时文件"
    assert c.get("/settings/avatar").json()["value"] == OLD_JPG


def test_avatar_clear_commit_failure_keeps_image_and_row(env, monkeypatch):
    c, Session, tmp_path = env
    old = c.put("/settings/avatar", json={"value": OLD_JPG}).json()["value"]

    state = _fail_commit(monkeypatch)
    r = c.put("/settings/avatar", json={"value": ""})
    state["on"] = False

    assert r.status_code == 503, r.text
    assert _db_value(Session, "ui_avatar") == old
    assert _avatar_files(tmp_path) == [old], "清空提交失败却删了图(不可恢复)"
    assert c.get("/settings/avatar").json()["value"] == OLD_JPG


def test_avatar_success_uses_unique_name_and_cleans_old(env):
    c, Session, tmp_path = env
    first = c.put("/settings/avatar", json={"value": OLD_JPG}).json()["value"]
    second = c.put("/settings/avatar", json={"value": NEW_PNG}).json()["value"]
    assert first != second, "固定文件名会互相覆盖(并发写入丢图)"
    assert second.startswith("avatar-") and second.endswith(".png")
    assert _avatar_files(tmp_path) == [second]
    assert _db_value(Session, "ui_avatar") == second
    assert c.get("/settings/avatar").json()["value"] == NEW_PNG
