"""个人资料头像: 文件系统与 DB 的一致性回归(2026-11-07)。

钉住的核心事实(真实文件 + 真实 SQLite 事务):
- 头像落盘用**唯一文件名**, 换头像/切扩展名不覆盖旧文件, 并发写不会互相踩;
- 顺序是 校验 → 写新文件 → commit DB → 提交成功后才删旧文件;
- commit 失败: 新文件被清掉、旧文件与 DB 原值不变(GET 仍返回旧图), 不谎报成功;
- 清空头像同样先提交 DB 再删文件, 提交失败时旧文件必须还在;
- 旧文件若仍被别的用户行引用(历史共享), 不删 —— 不能顺手删掉别人的头像;
- 非法 data URL 返回 400 且完全没有落盘副作用。
"""

from __future__ import annotations

import os

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import User
from src.web.api import auth, profile
from src.web.database import Base, get_db

_IMG_A = "data:image/png;base64,AAAA"
_IMG_B = "data:image/png;base64,BBBB"
_IMG_JPG = "data:image/jpeg;base64,CCCC"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """内存库 + 真实落盘目录(DATA_DIR=tmp_path) + 可注入 commit 失败。"""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    state: dict = {"fail_commit": False, "uid": "u-1"}

    class FlakySession(Session):
        def commit(self):  # noqa: D401 — 只注入一次失败, 其余照常
            if state["fail_commit"]:
                state["fail_commit"] = False
                raise RuntimeError("注入的提交失败")
            return super().commit()

    SessionF = sessionmaker(bind=engine, class_=FlakySession)

    s = SessionF()
    s.add_all(
        [
            User(id="u-1", username="alice", password_hash="x", role="member", is_active=True),
            User(id="u-2", username="bob", password_hash="x", role="member", is_active=True),
        ]
    )
    s.commit()
    s.close()

    app = FastAPI()
    app.include_router(profile.router, prefix="/api/profile")

    def _db():
        db = SessionF()
        try:
            yield db
        finally:
            db.close()

    # 与 get_db 同库同会话(FastAPI 依赖缓存) → 路由里的 commit 真正落到本会话
    def _current_user(db: Session = Depends(get_db)):
        return db.query(User).filter(User.id == state["uid"]).first()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[auth.get_current_user] = _current_user

    avatar_dir = os.path.join(str(tmp_path), "avatars")

    def files() -> list[str]:
        if not os.path.isdir(avatar_dir):
            return []
        return sorted(os.listdir(avatar_dir))

    def db_avatar(uid: str = "u-1"):
        db = SessionF()
        try:
            return db.query(User).filter(User.id == uid).first().avatar
        finally:
            db.close()

    def set_db_avatar(uid: str, fname: str | None) -> None:
        db = SessionF()
        try:
            db.query(User).filter(User.id == uid).first().avatar = fname
            db.commit()
        finally:
            db.close()

    return {
        "client": TestClient(app),
        "state": state,
        "files": files,
        "db_avatar": db_avatar,
        "set_db_avatar": set_db_avatar,
        "path": lambda f: os.path.join(avatar_dir, f),
    }


def test_avatar_unique_file_and_old_file_removed_after_commit(env):
    """正常换头像: 新文件名唯一, 旧文件在提交成功后删除, GET 读回新图。"""
    c = env["client"]
    assert c.put("/api/profile", json={"avatar": _IMG_A}).status_code == 200
    f1 = env["db_avatar"]()
    assert f1 and f1.startswith("avatar_u") and f1.endswith(".png")
    assert os.path.isfile(env["path"](f1))

    assert c.put("/api/profile", json={"avatar": _IMG_B}).status_code == 200
    f2 = env["db_avatar"]()
    assert f2 != f1, "换头像必须写新文件名, 不能原地覆盖"
    assert os.path.isfile(env["path"](f2))
    assert not os.path.exists(env["path"](f1)), "提交成功后旧文件应清理"
    assert env["files"]() == [f2], f"只应留下当前头像: {env['files']()}"
    assert c.get("/api/profile").json()["avatar"] == _IMG_B


def test_commit_failure_keeps_old_avatar_and_cleans_new_file(env):
    """commit 失败: 新文件被清掉, 旧文件与 DB 原值不变(GET 仍是旧图)。"""
    c = env["client"]
    c.put("/api/profile", json={"avatar": _IMG_A})
    f1 = env["db_avatar"]()
    assert os.path.isfile(env["path"](f1))

    env["state"]["fail_commit"] = True
    r = c.put("/api/profile", json={"avatar": _IMG_JPG, "nickname": "新昵称"})
    assert r.status_code == 503, r.text

    assert env["db_avatar"]() == f1, "DB 应回滚到旧文件名"
    assert os.path.isfile(env["path"](f1)), "旧头像文件不能被删"
    assert env["files"]() == [f1], f"新文件应被清理, 不留孤儿: {env['files']()}"
    body = c.get("/api/profile").json()
    assert body["avatar"] == _IMG_A, "GET 必须仍返回旧头像"
    assert body["nickname"] == "", "昵称也不应生效"


def test_clear_avatar_commit_failure_keeps_file(env):
    """清空头像 commit 失败: 文件必须还在, DB 仍指向它。"""
    c = env["client"]
    c.put("/api/profile", json={"avatar": _IMG_A})
    f1 = env["db_avatar"]()

    env["state"]["fail_commit"] = True
    assert c.put("/api/profile", json={"avatar": ""}).status_code == 503

    assert env["db_avatar"]() == f1
    assert os.path.isfile(env["path"](f1)), "清空失败时旧文件不能被提前删掉"
    assert c.get("/api/profile").json()["avatar"] == _IMG_A


def test_clear_avatar_success_removes_file(env):
    """清空头像成功: DB 置空 + 文件删除。"""
    c = env["client"]
    c.put("/api/profile", json={"avatar": _IMG_A})
    f1 = env["db_avatar"]()

    assert c.put("/api/profile", json={"avatar": ""}).status_code == 200
    assert env["db_avatar"]() in (None, "")
    assert not os.path.exists(env["path"](f1))
    assert env["files"]() == []
    assert c.get("/api/profile").json()["avatar"] == ""


def test_shared_legacy_avatar_file_not_deleted(env):
    """旧文件名仍被别的用户行引用(历史共享): 换头像时不得删它。"""
    c = env["client"]
    c.put("/api/profile", json={"avatar": _IMG_A})
    f1 = env["db_avatar"]()
    # 模拟历史数据: 另一个用户也指向同一个文件
    env["set_db_avatar"]("u-2", f1)

    assert c.put("/api/profile", json={"avatar": _IMG_B}).status_code == 200
    f2 = env["db_avatar"]()
    assert f2 != f1
    assert os.path.isfile(env["path"](f1)), "共享文件被误删了"

    # 共享解除(另一用户不再引用)后再换头像: 只清理"当前旧头像"(f2), 早先的共享文件
    # 已不是任何人的当前头像, 保持不动的保守行为(宁可留孤儿, 不可误删)
    env["set_db_avatar"]("u-2", None)
    assert c.put("/api/profile", json={"avatar": _IMG_JPG}).status_code == 200
    f3 = env["db_avatar"]()
    assert f3 not in (f1, f2)
    assert not os.path.exists(env["path"](f2))
    assert os.path.isfile(env["path"](f1))


def test_two_users_avatars_do_not_collide(env):
    """两个用户各写各的文件, 互不覆盖(唯一名 + 用户前缀)。"""
    c = env["client"]
    assert c.put("/api/profile", json={"avatar": _IMG_A}).status_code == 200
    u1_file = env["db_avatar"]("u-1")

    env["state"]["uid"] = "u-2"
    assert c.put("/api/profile", json={"avatar": _IMG_JPG}).status_code == 200
    u2_file = env["db_avatar"]("u-2")

    assert u1_file != u2_file
    assert os.path.isfile(env["path"](u1_file))
    assert os.path.isfile(env["path"](u2_file))
    assert env["db_avatar"]("u-1") == u1_file, "别人的头像记录不能被改"


def test_invalid_data_url_has_no_side_effect(env):
    """非法 data URL → 400, 旧头像文件与 DB 完全不变。"""
    c = env["client"]
    c.put("/api/profile", json={"avatar": _IMG_A})
    f1 = env["db_avatar"]()

    assert c.put("/api/profile", json={"avatar": "http://x/a.png"}).status_code == 400
    assert c.put("/api/profile", json={"avatar": "data:text/plain;base64,AAAA"}).status_code == 400
    assert c.put("/api/profile", json={"avatar": "data:image/png;base64,!!!==="}).status_code == 400

    assert env["db_avatar"]() == f1
    assert env["files"]() == [f1]


def test_no_staged_part_files_left_behind(env):
    """任何路径都不留 .part 暂存文件。"""
    c = env["client"]
    c.put("/api/profile", json={"avatar": _IMG_A})
    env["state"]["fail_commit"] = True
    c.put("/api/profile", json={"avatar": _IMG_B})
    assert all(not f.endswith(".part") for f in env["files"]()), env["files"]()
