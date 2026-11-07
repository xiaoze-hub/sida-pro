# -*- coding: utf-8 -*-
"""P0 旧版删用户端点 = 立即物理删除, 改为「禁用」的非破坏语义(2026-11-07)。

审计到的事实(改前):
`DELETE /api/auth/users/{user_id}` 直接 `db.delete(target)` + commit ——
生产是 PostgreSQL, `users.id` 被 13 张表 FK 引用、其中 22 处 `ondelete="CASCADE"`,
删 users 行会连带删掉该用户的持仓/账户/自选/通知渠道/对话/告警/会话/API Key;
本地 SQLite 没开 `PRAGMA foreign_keys`, 同一操作退化成留下一堆 `user_id` 悬空的
孤儿行 —— 两种结果都是**不可逆**的, 且:
- 无审计行(删完没有任何「谁删了谁」的证据)
- 无 30 天宽限期(与 `src/web/api/user_data.py` 的 GDPR 软删设计冲突,
  `/api/users/admin/{uid}/restore` 对已被硬删的用户完全失效)
- 不递增 token_version(踢人靠的是「用户行不存在」这种副作用)

本文件的验收线(真 app + 真 DB + 真 HTTP, 不打桩权限):
1. 非 owner 调该端点 403, 目标用户与数据一行不少
2. 无身份 401/403, 一行不少
3. owner 调该端点后: 用户行**仍在**, is_active=False, token_version 递增,
   is_deleted/deleted_at **保持未设置**(不触发 30 天物理清除)
4. 该用户全部关联数据一行不少(按所有带 user_id 的表逐一比对行数)
5. 账号立即失效: 旧 JWT 403, 再登录 403
6. 响应向后兼容: 仍是 200 + {"message": ...}(前端 authApi.deleteUser 只读 message)
7. 自我保护不变: 不能删/禁用自己(400), 不能动其它 owner(400)
8. 审计落库(manage_user), 且可再启用(PATCH is_active=true 恢复)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN_USER = "legacy_del_admin"
ADMIN_PW = "legacy_del_admin_pw_1234"
MEMBER_USER = "legacy_del_member"
MEMBER_PW = "legacy_del_member_pw_1234"
OWNER2_USER = "legacy_del_owner2"
OWNER2_PW = "legacy_del_owner2_pw_1234"


# ── fixtures ────────────────────────────────────────────────────────────
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    monkeypatch.setenv("AUTH_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AUTH_PASSWORD", ADMIN_PW)
    from src.web.database import SessionLocal
    from tests.conftest import purge_users

    db = SessionLocal()
    try:
        purge_users(db)
    finally:
        db.close()
    from src.web.app import app

    c = TestClient(app)
    try:
        yield c
    finally:
        c.cookies.clear()
        db = SessionLocal()
        try:
            purge_users(db)
        finally:
            db.close()


def _login(c: TestClient, username: str, password: str) -> str:
    r = c.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return _body(r)["token"]


def _body(r):
    """中间件可能把响应包在 {"data": ...} 里, 两种形态都吃。"""
    payload = r.json()
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def owner_token(client) -> str:
    return _login(client, ADMIN_USER, ADMIN_PW)


def _create_user(client: TestClient, owner_token: str, username: str, pw: str, role: str) -> str:
    r = client.post(
        "/api/auth/users",
        headers=_hdr(owner_token),
        json={"username": username, "password": pw, "role": role},
    )
    assert r.status_code == 200, r.text
    return _body(r)["user"]["id"]


@pytest.fixture()
def member(client, owner_token):
    """member 账号 + 一份「有血有肉」的关联数据。"""
    uid = _create_user(client, owner_token, MEMBER_USER, MEMBER_PW, "member")
    from src.db.models import (
        Account,
        ChatConversation,
        ChatMessage,
        Notification,
        NotifyChannel,
        PaperTradingAccount,
        Position,
        SkillApiKey,
        Stock,
        StockSuggestion,
    )
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        account = Account(user_id=uid, name="招商证券")
        stock = Stock(user_id=uid, symbol="600519", name="贵州茅台", market="CN")
        db.add_all([account, stock])
        db.commit()
        db.add(
            Position(
                user_id=uid,
                account_id=account.id,
                stock_id=stock.id,
                cost_price=1500.0,
                quantity=100,
                invested_amount=150000.0,
            )
        )
        db.add(NotifyChannel(user_id=uid, name="我的企业微信", type="wecom", config={}))
        db.add(Notification(user_id=uid, category="system", level="info", title="测试通知", body="x"))
        conv = ChatConversation(user_id=uid, title="茅台怎么看")
        db.add(conv)
        db.commit()
        db.add(ChatMessage(conversation_id=conv.id, role="user", content="分析一下"))
        db.add(
            StockSuggestion(
                user_id=uid,
                stock_symbol="600519",
                stock_market="CN",
                action="buy",
                action_label="建仓",
                agent_name="daily_report",
            )
        )
        db.add(PaperTradingAccount(user_id=uid))
        db.add(
            SkillApiKey(
                user_id=uid,
                key_hash="hash-" + uid[:8],
                key_prefix="sk-test",
                tier="free",
                status="active",
            )
        )
        db.commit()
    finally:
        db.close()
    return {"id": uid, "password": MEMBER_PW}


# ── 快照: 所有带 user_id 的表 ────────────────────────────────────────────
def _user_scoped_counts(uid: str) -> dict[str, int]:
    from sqlalchemy import func, select

    from src.web.database import Base, SessionLocal

    out: dict[str, int] = {}
    db = SessionLocal()
    try:
        for table in Base.metadata.sorted_tables:
            if "user_id" not in table.c:
                continue
            n = db.execute(
                select(func.count()).select_from(table).where(table.c.user_id == uid)
            ).scalar()
            if n:
                out[table.name] = int(n)
    finally:
        db.close()
    return out


def _user_row(uid: str):
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == uid).first()
    finally:
        db.close()


def _chat_rows(uid: str) -> tuple[int, int]:
    from src.web.database import SessionLocal
    from src.web.models import ChatConversation, ChatMessage

    db = SessionLocal()
    try:
        convs = db.query(ChatConversation.id).filter(ChatConversation.user_id == uid).all()
        ids = [c[0] for c in convs]
        msgs = (
            db.query(ChatMessage).filter(ChatMessage.conversation_id.in_(ids)).count() if ids else 0
        )
        return len(ids), msgs
    finally:
        db.close()


def _audit_detail(substr: str) -> list[str]:
    from src.web.database import SessionLocal
    from src.web.models import AuditLog

    db = SessionLocal()
    try:
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.action == "manage_user")
            .order_by(AuditLog.id.desc())
            .limit(200)
            .all()
        )
        return [r.detail or "" for r in rows if substr in (r.detail or "")]
    finally:
        db.close()


# ── 用例 ────────────────────────────────────────────────────────────────
def test_member_cannot_delete_user(client, owner_token, member):
    """非 owner 一律 403, 数据一行不能动(横向越权)。"""
    member_token = _login(client, MEMBER_USER, MEMBER_PW)
    before = _user_scoped_counts(member["id"])

    r = client.delete(f"/api/auth/users/{member['id']}", headers=_hdr(member_token))
    assert r.status_code == 403, r.text
    row = _user_row(member["id"])
    assert row is not None and row.is_active is True
    assert _user_scoped_counts(member["id"]) == before


def test_anonymous_cannot_delete_user(client, member):
    """无身份 401/403。"""
    before = _user_scoped_counts(member["id"])

    r = client.delete(f"/api/auth/users/{member['id']}")
    assert r.status_code in (401, 403), r.text
    assert _user_row(member["id"]) is not None
    assert _user_scoped_counts(member["id"]) == before


def test_owner_delete_disables_but_preserves_everything(client, owner_token, member):
    """核心: owner 调 DELETE 后账号被禁用, 用户行与全部关联数据**保留**。"""
    uid = member["id"]
    member_token = _login(client, MEMBER_USER, MEMBER_PW)
    before_counts = _user_scoped_counts(uid)
    before_convs, before_msgs = _chat_rows(uid)
    # 夹具至少种下这些归属行, 否则本用例的空洞会假绿
    for t in ("accounts", "stocks", "positions", "notify_channels", "notifications",
              "stock_suggestions", "paper_trading_account", "skill_api_keys"):
        assert before_counts.get(t), f"夹具缺 {t} 行"
    # 注: chat_messages 没有 users FK, 共享测试库可能残留同 conversation_id 的旧消息,
    # 所以这里只钉「有消息」, 真正的判据是下面的操作前后等值。
    assert before_convs == 1 and before_msgs >= 1

    row_before = _user_row(uid)
    ver_before = int(row_before.token_version or 1)

    r = client.delete(f"/api/auth/users/{uid}", headers=_hdr(owner_token))
    assert r.status_code == 200, r.text
    body = _body(r)
    # 向后兼容: 前端 authApi.deleteUser 的返回类型就是 { message: string }
    assert isinstance(body.get("message"), str) and body["message"]
    assert body.get("deleted") is False
    assert body.get("status") == "disabled"

    row = _user_row(uid)
    assert row is not None, "用户行被物理删除 —— 关联数据已不可恢复"
    assert row.is_active is False
    assert int(row.token_version) == ver_before + 1
    # 不进入 30 天物理清除队列(那是另一条需要用户密码确认的 GDPR 路径)
    assert bool(row.is_deleted) is False
    assert row.deleted_at is None

    after_counts = _user_scoped_counts(uid)
    assert after_counts == before_counts, f"关联数据被删: {before_counts} → {after_counts}"
    assert _chat_rows(uid) == (before_convs, before_msgs)

    # 账号立即失效: 旧 JWT 403, 再登录 403
    r = client.get("/api/auth/me", headers=_hdr(member_token))
    assert r.status_code == 403, r.text
    r = client.post(
        "/api/auth/login", json={"username": MEMBER_USER, "password": MEMBER_PW}
    )
    assert r.status_code == 403, r.text

    # 审计留痕: 谁禁用了谁
    details = _audit_detail(MEMBER_USER)
    assert any("禁用" in d or "disable" in d.lower() for d in details), details[:5]

    # 可逆: owner 用 PATCH is_active=true 立刻恢复(数据仍在, 无需恢复备份)
    r = client.patch(
        f"/api/auth/users/{uid}", headers=_hdr(owner_token), json={"is_active": True}
    )
    assert r.status_code == 200, r.text
    assert _user_row(uid).is_active is True
    assert _user_scoped_counts(uid) == before_counts


def test_delete_is_idempotent_and_never_hard_deletes(client, owner_token, member):
    """重复调用仍不删数据(前端连点/重试不得造成二次破坏)。"""
    uid = member["id"]
    before = _user_scoped_counts(uid)

    for _ in range(3):
        r = client.delete(f"/api/auth/users/{uid}", headers=_hdr(owner_token))
        assert r.status_code == 200, r.text
        assert _user_row(uid) is not None
        assert _user_scoped_counts(uid) == before


def test_cannot_delete_self_or_other_owner(client, owner_token):
    """自我保护不变: 不能禁自己 / 不能动其它 owner。"""
    me = _body(client.get("/api/auth/me", headers=_hdr(owner_token)))
    my_id = me.get("id") or (me.get("user") or {}).get("id")

    if my_id:
        r = client.delete(f"/api/auth/users/{my_id}", headers=_hdr(owner_token))
        assert r.status_code == 400, r.text

    owner2_id = _create_user(client, owner_token, OWNER2_USER, OWNER2_PW, "owner")
    r = client.delete(f"/api/auth/users/{owner2_id}", headers=_hdr(owner_token))
    assert r.status_code == 400, r.text
    row = _user_row(owner2_id)
    assert row is not None and row.is_active is True and row.role == "owner"


def test_unknown_user_404(client, owner_token):
    r = client.delete("/api/auth/users/00000000-0000-0000-0000-000000000000",
                      headers=_hdr(owner_token))
    assert r.status_code == 404, r.text
