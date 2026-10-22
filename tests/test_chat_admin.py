# -*- coding: utf-8 -*-
"""会话管理接口测试(2026-10-22): src/web/api/chat_admin.py

覆盖 4 个端点:
1. PATCH /chat/conversations/{cid} —— 重命名成功 / 空标题 400 / 越权 404
2. GET  /chat/conversations?q=     —— 命中标题 / 命中消息正文 / 越权不泄露
3. DELETE /chat/conversations/{cid}/messages/{mid} —— 删成功列表少一条 / 删他人 404
4. GET  /chat/conversations/{cid}/runs —— 返回该会话轨迹 / 越权 404

隔离: 全部用 SQLite 内存库(StaticPool 共享同一条连接), 自建 FastAPI app 只挂
chat_admin router, **不连生产库**; 用户身份用 dependency_overrides 注入。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.models import (
    Base,
    ChatConversation,
    ChatMessage,
    CopilotRun,
)
from src.web.database import get_db


# ── 内存库 + app ────────────────────────────────────────────────────────────

@pytest.fixture()
def db_session():
    """SQLite 内存库(StaticPool: TestClient 在工作线程跑端点, 必须共享同一连接)。"""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


class _FakeUser:
    """替身用户对象: 端点只读 .id / .role。

    id 指向库里的**真实 User 行**(chat_conversations.user_id 有 FK 到 users.id,
    不建真行会撞 FOREIGN KEY constraint failed)。
    """

    def __init__(self, uid: str, role: str = "member"):
        self.id = uid
        self.role = role
        self.username = f"user_{uid[:8]}"
        self.is_active = True
        self.token_version = 0


@pytest.fixture()
def users(db_session):
    """建两个真实 User 行(过 FK), 返回它们的替身对象。"""
    from src.db.models import User

    out = {}
    for key in ("a", "b"):
        uid = f"user-{key}-{uuid.uuid4().hex[:8]}"
        db_session.add(
            User(
                id=uid,
                username=f"chatadmin_{key}_{uuid.uuid4().hex[:8]}",
                password_hash="x",
                role="member",
            )
        )
        db_session.commit()
        out[key] = _FakeUser(uid)
    return out


@pytest.fixture()
def client(db_session, users):
    """只挂 chat_admin 的最小 app; 鉴权用 dependency_overrides 替身。"""
    from src.web.api import chat_admin
    from src.web.api.auth import get_current_user

    app = FastAPI()
    app.include_router(chat_admin.router, prefix="/api/chat")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: users["a"]

    with TestClient(app) as c:
        c.users = users
        yield c


@pytest.fixture()
def client_b(db_session, users):
    """同一套数据, 但以用户 B 身份访问(越权用例)。"""
    from src.web.api import chat_admin
    from src.web.api.auth import get_current_user

    app = FastAPI()
    app.include_router(chat_admin.router, prefix="/api/chat")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: users["b"]

    with TestClient(app) as c:
        c.users = users
        yield c


# ── 数据工厂 ────────────────────────────────────────────────────────────────

def _mk_conversation(db, user, title: str, *, pinned: bool = False,
                     symbol: str | None = None) -> ChatConversation:
    conv = ChatConversation(
        user_id=user.id,
        title=title,
        stock_symbol=symbol,
        stock_market="SH" if symbol else None,
        pinned=pinned,
        created_at=datetime(2026, 10, 1, 9, 0, 0),
        updated_at=datetime(2026, 10, 1, 9, 0, 0),
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _mk_message(db, conv, role: str, content: str) -> ChatMessage:
    msg = ChatMessage(
        conversation_id=conv.id,
        role=role,
        content=content,
        created_at=datetime(2026, 10, 1, 9, 1, 0),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return msg


def _mk_run(db, conv, user, *, recipe: str = "dossier",
             status: str = "ok", tool_calls: int = 3,
             elapsed_ms: int = 1200, minutes_ago: int = 0) -> CopilotRun:
    run = CopilotRun(
        run_id=str(uuid.uuid4()),
        conversation_id=conv.id,
        user_id=user.id,
        recipe=recipe,
        question="分析一下",
        status=status,
        tool_calls=tool_calls,
        elapsed_ms=elapsed_ms,
        created_at=datetime(2026, 10, 1, 9, 0, 0) - timedelta(minutes=minutes_ago),
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


# ── 1. PATCH 重命名 / 置顶 ──────────────────────────────────────────────────

class TestUpdateConversation:
    def test_rename_success(self, client, db_session, users):
        conv = _mk_conversation(db_session, users["a"], "旧标题")

        r = client.patch(f"/api/chat/conversations/{conv.id}", json={"title": "  新标题  "})
        assert r.status_code == 200, r.text
        body = r.json()
        # 标题去首尾空白
        assert body["title"] == "新标题"

        db_session.expire_all()
        assert db_session.get(ChatConversation, conv.id).title == "新标题"

    def test_rename_empty_title_400(self, client, db_session, users):
        conv = _mk_conversation(db_session, users["a"], "标题")

        for bad in ("", "   "):
            r = client.patch(f"/api/chat/conversations/{conv.id}", json={"title": bad})
            assert r.status_code == 400, f"空标题应 400, 实得 {r.status_code}"

        # 400 不得改到库里的标题
        db_session.expire_all()
        assert db_session.get(ChatConversation, conv.id).title == "标题"

    def test_rename_too_long_400(self, client, db_session, users):
        conv = _mk_conversation(db_session, users["a"], "标题")
        r = client.patch(f"/api/chat/conversations/{conv.id}", json={"title": "x" * 81})
        assert r.status_code == 400

    def test_rename_max_len_ok(self, client, db_session, users):
        conv = _mk_conversation(db_session, users["a"], "标题")
        r = client.patch(f"/api/chat/conversations/{conv.id}", json={"title": "y" * 80})
        assert r.status_code == 200, r.text
        assert r.json()["title"] == "y" * 80

    def test_pin_toggle(self, client, db_session, users):
        conv = _mk_conversation(db_session, users["a"], "标题")

        r = client.patch(f"/api/chat/conversations/{conv.id}", json={"pinned": True})
        assert r.status_code == 200, r.text
        assert r.json()["pinned"] is True

        db_session.expire_all()
        assert bool(db_session.get(ChatConversation, conv.id).pinned) is True

        r = client.patch(f"/api/chat/conversations/{conv.id}", json={"pinned": False})
        assert r.status_code == 200
        assert r.json()["pinned"] is False

    def test_other_user_conversation_404(self, client, db_session, users):
        """越权: 用户 A 改用户 B 的会话 → 404(不是 403, 防账号探测)。"""
        conv_b = _mk_conversation(db_session, users["b"], "B的会话")

        r = client.patch(f"/api/chat/conversations/{conv_b.id}", json={"title": "改我的"})
        assert r.status_code == 404, r.text

        db_session.expire_all()
        assert db_session.get(ChatConversation, conv_b.id).title == "B的会话"

    def test_missing_conversation_404(self, client):
        r = client.patch("/api/chat/conversations/999999", json={"title": "x"})
        assert r.status_code == 404


# ── 2. GET 搜索 ────────────────────────────────────────────────────────────

class TestSearchConversations:
    def test_search_hits_title(self, client, db_session, users):
        _mk_conversation(db_session, users["a"], "北方稀土建仓分析")
        _mk_conversation(db_session, users["a"], "完全无关的标题")

        r = client.get("/api/chat/conversations", params={"q": "北方稀土"})
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["title"] == "北方稀土建仓分析"

    def test_search_hits_message_content(self, client, db_session, users):
        conv = _mk_conversation(db_session, users["a"], "标题甲")
        _mk_message(db_session, conv, "user", "帮我看看三花智控的走势")
        _mk_conversation(db_session, users["a"], "标题乙")

        r = client.get("/api/chat/conversations", params={"q": "三花智控"})
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["id"] == conv.id
        # 命中消息的会话也要带消息数
        assert rows[0]["message_count"] == 1

    def test_search_does_not_leak_other_users(self, client, db_session, users):
        """A 搜 B 的会话标题 → 命中不到(B 的会话不在 A 的结果里)。"""
        conv_b = _mk_conversation(db_session, users["b"], "机密涨停标的")
        _mk_message(db_session, conv_b, "user", "内部消息关键词")

        r = client.get("/api/chat/conversations", params={"q": "机密涨停标的"})
        assert r.status_code == 200
        assert r.json() == []

        r = client.get("/api/chat/conversations", params={"q": "内部消息关键词"})
        assert r.status_code == 200
        assert r.json() == []

    def test_search_empty_q_lists_own_only(self, client, db_session, users):
        _mk_conversation(db_session, users["a"], "A1")
        _mk_conversation(db_session, users["a"], "A2")
        _mk_conversation(db_session, users["b"], "B1")

        r = client.get("/api/chat/conversations", params={"q": ""})
        assert r.status_code == 200
        titles = {row["title"] for row in r.json()}
        assert titles == {"A1", "A2"}

    def test_search_pinned_first(self, client, db_session, users):
        _mk_conversation(db_session, users["a"], "普通会话")
        _mk_conversation(db_session, users["a"], "置顶会话", pinned=True)

        r = client.get("/api/chat/conversations", params={"q": "会话"})
        assert r.status_code == 200
        rows = r.json()
        assert rows[0]["title"] == "置顶会话"
        assert rows[0]["pinned"] is True
        assert rows[1]["pinned"] is False

    def test_search_result_fields(self, client, db_session, users):
        conv = _mk_conversation(db_session, users["a"], "带标的的会话", symbol="600111")
        _mk_message(db_session, conv, "user", "你好")

        r = client.get("/api/chat/conversations", params={"q": "带标的"})
        assert r.status_code == 200
        row = r.json()[0]
        for key in ("id", "title", "stock_symbol", "pinned", "updated_at", "message_count"):
            assert key in row, f"搜索结果缺字段 {key}"
        assert row["stock_symbol"] == "600111"
        assert row["message_count"] == 1


# ── 3. DELETE 单条消息 ─────────────────────────────────────────────────────

class TestDeleteMessage:
    def test_delete_message_reduces_list(self, client, db_session, users):
        conv = _mk_conversation(db_session, users["a"], "会话")
        m1 = _mk_message(db_session, conv, "user", "问题一")
        m2 = _mk_message(db_session, conv, "assistant", "回答二")

        r = client.delete(f"/api/chat/conversations/{conv.id}/messages/{m1.id}")
        assert r.status_code == 200, r.text

        db_session.expire_all()
        rows = (
            db_session.query(ChatMessage)
            .filter(ChatMessage.conversation_id == conv.id)
            .order_by(ChatMessage.id)
            .all()
        )
        assert [m.id for m in rows] == [m2.id]

    def test_delete_other_user_message_404(self, client_b, db_session, users):
        """B 删 A 会话里的消息 → 404, 且消息不能被删掉。"""
        conv_a = _mk_conversation(db_session, users["a"], "A的会话")
        msg_a = _mk_message(db_session, conv_a, "user", "A的问题")

        r = client_b.delete(f"/api/chat/conversations/{conv_a.id}/messages/{msg_a.id}")
        assert r.status_code == 404, r.text

        db_session.expire_all()
        assert db_session.get(ChatMessage, msg_a.id) is not None

    def test_delete_message_not_in_conversation_404(self, client, db_session, users):
        """消息属于别的会话 → 404(会话归属校验通过, 消息归属校验失败)。"""
        conv_a = _mk_conversation(db_session, users["a"], "会话A")
        conv_b = _mk_conversation(db_session, users["a"], "会话B")
        msg_in_b = _mk_message(db_session, conv_b, "user", "在B里的消息")

        r = client.delete(f"/api/chat/conversations/{conv_a.id}/messages/{msg_in_b.id}")
        assert r.status_code == 404, r.text

        db_session.expire_all()
        assert db_session.get(ChatMessage, msg_in_b.id) is not None

    def test_delete_message_other_conversation_of_other_user_404(
        self, client_b, db_session, users
    ):
        """B 拿自己的会话 id 去删 A 的消息 → 404。"""
        conv_a = _mk_conversation(db_session, users["a"], "A的会话")
        msg_a = _mk_message(db_session, conv_a, "user", "A的问题")
        conv_b = _mk_conversation(db_session, users["b"], "B的会话")

        r = client_b.delete(f"/api/chat/conversations/{conv_b.id}/messages/{msg_a.id}")
        assert r.status_code == 404, r.text

        db_session.expire_all()
        assert db_session.get(ChatMessage, msg_a.id) is not None


# ── 4. GET 会话轨迹 ────────────────────────────────────────────────────────

class TestConversationRuns:
    def test_runs_return_conversation_trace(self, client, db_session, users):
        conv = _mk_conversation(db_session, users["a"], "会话")
        run = _mk_run(
            db_session, conv, users["a"],
            recipe="dossier", status="ok", tool_calls=5, elapsed_ms=3300,
        )

        r = client.get(f"/api/chat/conversations/{conv.id}/runs")
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1
        item = rows[0]
        for key in ("run_id", "recipe", "status", "tool_calls", "elapsed_ms", "created_at"):
            assert key in item, f"轨迹缺字段 {key}"
        assert item["run_id"] == run.run_id
        assert item["recipe"] == "dossier"
        assert item["status"] == "ok"
        assert item["tool_calls"] == 5
        assert item["elapsed_ms"] == 3300

    def test_runs_exclude_other_conversations(self, client, db_session, users):
        conv_a = _mk_conversation(db_session, users["a"], "会话A")
        conv_b = _mk_conversation(db_session, users["a"], "会话B")
        run_a = _mk_run(db_session, conv_a, users["a"], minutes_ago=0)
        _mk_run(db_session, conv_b, users["a"], minutes_ago=5)

        r = client.get(f"/api/chat/conversations/{conv_a.id}/runs")
        assert r.status_code == 200
        rows = r.json()
        assert len(rows) == 1
        assert rows[0]["run_id"] == run_a.run_id

    def test_runs_other_user_conversation_404(self, client_b, db_session, users):
        conv_a = _mk_conversation(db_session, users["a"], "A的会话")
        _mk_run(db_session, conv_a, users["a"])

        r = client_b.get(f"/api/chat/conversations/{conv_a.id}/runs")
        assert r.status_code == 404, r.text


# ── 路由注册顺序(回归护栏) ─────────────────────────────────────────────────

class TestRouteRegistrationOrder:
    """搜索端点与 chat.py 的旧列表端点**同路径**(GET /api/chat/conversations)。

    FastAPI 按注册顺序匹配 → app.py 里 chat_admin 必须挂在 chat 之前,
    否则旧列表端点会遮住搜索端点(q 参数被静默忽略, 搜索功能"看起来还在"
    其实永远返回全量列表)。这个用例把该约束钉死。
    """

    def _build(self, db_session, users, admin_first: bool):
        from fastapi import FastAPI

        from src.web.api import chat, chat_admin
        from src.web.api.auth import get_current_user

        app = FastAPI()
        if admin_first:
            app.include_router(chat_admin.router, prefix="/api/chat")
            app.include_router(chat.router, prefix="/api/chat")
        else:
            app.include_router(chat.router, prefix="/api/chat")
            app.include_router(chat_admin.router, prefix="/api/chat")
        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_current_user] = lambda: users["a"]
        return TestClient(app)

    def test_search_endpoint_must_be_mounted_before_legacy_list(self, db_session, users):
        conv = _mk_conversation(db_session, users["a"], "顺序探针会话")
        _mk_conversation(db_session, users["a"], "无关会话")

        # 正确顺序: 搜索端点命中(q 生效, 返回 pinned/message_count)
        good = self._build(db_session, users, admin_first=True)
        r = good.get("/api/chat/conversations", params={"q": "探针"})
        assert r.status_code == 200, r.text
        rows = r.json()
        assert len(rows) == 1, "q 参数未生效 —— 搜索端点被旧列表端点遮住了"
        assert rows[0]["id"] == conv.id
        assert "pinned" in rows[0]

        # 反证: 错误顺序时旧端点会遮住搜索端点(q 被忽略, 返回全量)
        bad = self._build(db_session, users, admin_first=False)
        r = bad.get("/api/chat/conversations", params={"q": "探针"})
        assert r.status_code == 200
        assert len(r.json()) == 2, "旧列表端点不应过滤 q —— 若此断言失败说明框架行为变了"

    def test_app_registers_chat_admin_before_chat(self):
        """真实 app 的注册顺序: chat_admin 在 chat 之前。

        判据用 `include_context`(本 FastAPI 版本把 include_router 包成
        _IncludedRouter, 路由对象自身的 prefix/tags 都是空的)。
        """
        from src.web.app import app

        order: list[str] = []
        for r in app.routes:
            ctx = getattr(r, "include_context", None)
            if ctx is None:
                continue
            if getattr(ctx, "prefix", None) != "/api/chat":
                continue
            tags = {str(t) for t in (getattr(ctx, "tags", None) or [])}
            if "chat-admin" in tags:
                order.append("chat_admin")
            elif "chat" in tags:
                order.append("chat")

        assert order, "没在 app.routes 里找到 /api/chat 的注册记录 —— 框架结构变了"
        assert "chat_admin" in order, "chat_admin 未注册"
        assert "chat" in order, "chat 未注册"
        assert order.index("chat_admin") < order.index("chat"), (
            f"chat_admin 必须先于 chat 注册, 实际顺序={order}"
        )

    def test_real_app_search_is_not_shadowed(self, db_session, users):
        """真实 app 上 GET /api/chat/conversations?q= 必须走搜索端点。

        判据: 响应带 pinned/updated_at/message_count —— 旧列表端点没有这三个字段。
        """
        from src.web.app import app
        from src.web.api.auth import get_current_user

        conv = _mk_conversation(db_session, users["a"], "真实app探针")
        _mk_conversation(db_session, users["a"], "真实app无关")

        app.dependency_overrides[get_db] = lambda: db_session
        app.dependency_overrides[get_current_user] = lambda: users["a"]
        try:
            with TestClient(app) as c:
                r = c.get("/api/chat/conversations", params={"q": "真实app探针"})
                assert r.status_code == 200, r.text
                data = r.json()
                rows = data.get("data", data)
                assert len(rows) == 1, f"q 未生效(被旧列表端点遮住): {rows}"
                assert rows[0]["id"] == conv.id
                for key in ("pinned", "updated_at", "message_count"):
                    assert key in rows[0], f"响应缺 {key} —— 命中的是旧列表端点"
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_current_user, None)


# ── 迁移 ────────────────────────────────────────────────────────────────────

class TestPinnedMigration:
    def test_migration_adds_pinned_column(self, db_session):
        """v217 迁移幂等: 缺列时补上, 已存在时跳过。"""
        from sqlalchemy import text

        from src.web.migrations import _m217_chat_conversation_pinned

        conn = db_session.connection()
        # 先造一张"没有 pinned 列"的旧表
        conn.execute(text("DROP TABLE chat_conversations"))
        conn.execute(text(
            "CREATE TABLE chat_conversations ("
            "id INTEGER PRIMARY KEY, title TEXT DEFAULT '', user_id TEXT)"
        ))
        conn.commit()

        _m217_chat_conversation_pinned(conn)
        rows = conn.execute(text("PRAGMA table_info(chat_conversations)")).fetchall()
        cols = {r[1]: r for r in rows}
        assert "pinned" in cols
        # 布尔列带 server_default: 存量行(不给值)插入后默认不置顶
        conn.execute(text(
            "INSERT INTO chat_conversations (id, title, user_id) VALUES (1, '旧会话', 'u1')"
        ))
        conn.commit()
        val = conn.execute(text("SELECT pinned FROM chat_conversations WHERE id=1")).scalar()
        assert val in (0, False)

        # 幂等: 再跑一次不报错
        _m217_chat_conversation_pinned(conn)

    def test_migration_version_is_contiguous(self):
        """迁移版本号连续(门禁 scripts/check_migrations.py 的口径)。"""
        from src.web.migrations import MIGRATIONS

        versions = [m.version for m in MIGRATIONS]
        assert versions == sorted(versions)
        for prev, cur in zip(versions, versions[1:]):
            assert cur == prev + 1, f"迁移版本不连续: {prev} -> {cur}"
        assert versions[-1] == 218
