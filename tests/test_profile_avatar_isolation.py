# -*- coding: utf-8 -*-
"""个人头像多账号隔离(2026-11-07)。

前端头像曾读写全局设置 `/settings/avatar`(`ui_avatar` 是**全局单行**), 任何账号看到的
都是同一个人 —— 典型的多账号串头像。个人头像的正确载体是 `GET/PUT /api/profile`
(`users.avatar`, 按用户隔离)。本文件钉住后端这一侧:

1. 每个账号只读回自己的头像, 没设置过就是空串;
2. B 上传/清空不影响 A 的文件与返回值;
3. 磁盘文件名按 user_id 隔离, DB 只存文件名(不塞 base64)。
"""
from __future__ import annotations

import base64
import os

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.web import models as M  # noqa: F401 (确保模型注册到 Base)
from src.web.api import profile as profile_api
from src.web.api.auth import get_current_user
from src.web.database import Base, get_db
from src.web.models import User


def _data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


AVATAR_ALICE = _data_url(b"alice-avatar-bytes")
AVATAR_BOB = _data_url(b"bob-avatar-bytes", mime="image/jpeg")


def _client(tmp_path, monkeypatch):
    """两个普通账号 + 可切换的"当前用户"依赖, 走真实的 profile 路由/落盘逻辑。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session_ = sessionmaker(bind=engine)

    seed = Session_()
    seed.add_all(
        [
            User(id="u-alice", username="alice", password_hash="x", role="member"),
            User(id="u-bob", username="bob", password_hash="x", role="member"),
        ]
    )
    seed.commit()
    seed.close()

    app = FastAPI()
    app.include_router(profile_api.router, prefix="/profile")
    current = {"username": "alice"}

    def _db():
        s = Session_()
        try:
            yield s
        finally:
            s.close()

    def _current_user(db: Session = Depends(get_db)) -> User:
        # 用 get_db 这一符号取 session(依赖缓存按原始 callable 命中), 与路由共用
        # 同一次请求的 session —— 否则 db.refresh(user) 会撞 "not persistent"。
        return db.query(User).filter(User.username == current["username"]).one()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _current_user
    return TestClient(app), current, Session_


def test_avatar_is_read_per_account_not_globally(tmp_path, monkeypatch):
    client, current, _ = _client(tmp_path, monkeypatch)

    r = client.put("/profile", json={"avatar": AVATAR_ALICE})
    assert r.status_code == 200, r.text
    assert r.json()["avatar"] == AVATAR_ALICE

    assert client.get("/profile").json()["avatar"] == AVATAR_ALICE

    # 换成 bob: 没设置过头像 ⇒ 空串, 绝不能看到 alice 的图
    current["username"] = "bob"
    assert client.get("/profile").json()["avatar"] == ""


def test_second_account_upload_does_not_affect_first(tmp_path, monkeypatch):
    client, current, _ = _client(tmp_path, monkeypatch)

    client.put("/profile", json={"avatar": AVATAR_ALICE})
    current["username"] = "bob"
    r = client.put("/profile", json={"avatar": AVATAR_BOB})
    assert r.status_code == 200, r.text
    assert r.json()["avatar"] == AVATAR_BOB

    current["username"] = "alice"
    assert client.get("/profile").json()["avatar"] == AVATAR_ALICE

    current["username"] = "bob"
    assert client.get("/profile").json()["avatar"] == AVATAR_BOB


def test_clear_only_removes_own_avatar_file(tmp_path, monkeypatch):
    client, current, _ = _client(tmp_path, monkeypatch)

    client.put("/profile", json={"avatar": AVATAR_ALICE})
    current["username"] = "bob"
    client.put("/profile", json={"avatar": AVATAR_BOB})

    current["username"] = "alice"
    r = client.put("/profile", json={"avatar": ""})
    assert r.status_code == 200, r.text
    assert client.get("/profile").json()["avatar"] == ""

    # bob 的头像文件不该被 alice 的清空带走
    current["username"] = "bob"
    assert client.get("/profile").json()["avatar"] == AVATAR_BOB

    files = sorted(os.listdir(os.path.join(str(tmp_path), "avatars")))
    assert "avatar_uu-bob.jpg" in files
    assert not any("alice" in name for name in files)


def test_db_stores_filename_only_per_user(tmp_path, monkeypatch):
    client, current, Session_ = _client(tmp_path, monkeypatch)

    client.put("/profile", json={"avatar": AVATAR_ALICE})
    current["username"] = "bob"
    client.put("/profile", json={"avatar": AVATAR_BOB})

    db = Session_()
    try:
        rows = {u.username: u.avatar for u in db.query(User).all()}
    finally:
        db.close()

    assert rows["alice"] == "avatar_uu-alice.png"
    assert rows["bob"] == "avatar_uu-bob.jpg"
    # 只存文件名, 不把 data URL 塞进 users 行
    assert all(not value.startswith("data:") for value in rows.values())
