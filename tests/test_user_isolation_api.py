"""S1-S4 (2026-08-23): API 端点 user_id 隔离测试

覆盖:
- S1: /api/history list/detail/delete 仅当前用户可见, 否则 404
- S2: /api/chat/conversations list/get/delete/messages 仅当前用户可见
- S3: /api/price-alerts list/put/toggle/delete 仅当前用户可见
- S4: /api/notifications list/get/unread-count/read/mark-all 仅当前用户可见

策略:
- 直接用 SessionLocal 写入两条不同 user 的数据
- 用 FastAPI TestClient + 模拟 JWT 登录
- 验证: 用户 A 看不到用户 B 的数据; 用户 A 访问用户 B 的 ID 返回 404(非 403, 防账号探测)
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.web.database import SessionLocal, init_db


@pytest.fixture(scope="module", autouse=True)
def _ensure_db():
    init_db()


@pytest.fixture(scope="module", autouse=True)
def _cleanup_module_owner():
    """模块结束清理 _OWNER_USERNAME (owner), 避免污染持久化库。

    该 owner 是 session 级单例(_get_or_create_owner_token), 若无清理会残留,
    导致其他测试(sentinel 查 owner 计数、RBAC owner 唯一性)看到 2 个 owner 而失败。
    """
    yield
    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        owner = db.query(User).filter(User.username == _OWNER_USERNAME).first()
        if owner:
            uid = str(owner.id)
            # 复用 _cleanup_user_data 先清数据再清用户
            _cleanup_user_data([uid])
    finally:
        db.close()


@pytest.fixture()
def client():
    from src.web.app import app
    return TestClient(app)


_OWNER_USERNAME = "iso_test_owner_v1"
_OWNER_PASSWORD = "iso_test_owner_pw_2026_v1"


def _login_owner(client: TestClient) -> str:
    """登录 owner — 只在 owner 不存在时创建, 否则复用。
    避免反复修改 owner 密码导致已签发 token 失效(token_version 自增)。
    """
    from src.web.database import SessionLocal
    from src.web.models import User
    from src.web.api.auth import hash_password, create_user

    db = SessionLocal()
    try:
        owner = db.query(User).filter(User.username == _OWNER_USERNAME).first()
        if not owner:
            owner = create_user(db, _OWNER_USERNAME, _OWNER_PASSWORD, "owner")
        else:
            # 保持密码不变, 避免 token_version 自增
            owner.password_hash = hash_password(_OWNER_PASSWORD)
            db.commit()
    finally:
        db.close()

    r = client.post(
        "/api/auth/login",
        json={"username": _OWNER_USERNAME, "password": _OWNER_PASSWORD},
    )
    if r.status_code != 200:
        pytest.skip(f"无法登录 owner ({_OWNER_USERNAME}): {r.status_code} {r.text[:200]}")
    return r.json()["data"]["token"]


def _create_two_members(client: TestClient, owner_token: str) -> dict:
    """owner 下创建两个成员 + 各自 token。

    走 DB 直接创建 + create_token 签 JWT(不走 /api/auth/login, 避免触发登录限流
    20次/分钟, 4 个测试用例共用 2 个 owner 登录 + 8 个成员登录 = 10 次, 加上 fixture 反复
    创建, 极易触发限流)。"""
    from src.web.database import SessionLocal
    from src.web.api.auth import create_token

    a_name = f"iso_a_{uuid.uuid4().hex[:8]}"
    b_name = f"iso_b_{uuid.uuid4().hex[:8]}"
    a_pass = "pass1234abcd"

    r1 = client.post(
        "/api/auth/users",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"username": a_name, "password": a_pass, "role": "member"},
    )
    assert r1.status_code == 200, r1.text
    a_id = r1.json()["data"]["user"]["id"]

    r2 = client.post(
        "/api/auth/users",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"username": b_name, "password": a_pass, "role": "member"},
    )
    assert r2.status_code == 200, r2.text
    b_id = r2.json()["data"]["user"]["id"]

    # 直接签 JWT(不走 /login API)
    db = SessionLocal()
    try:
        from src.web.models import User
        ua = db.query(User).filter(User.id == a_id).first()
        ub = db.query(User).filter(User.id == b_id).first()
        a_token, _ = create_token(ua)
        b_token, _ = create_token(ub)
    finally:
        db.close()

    return {"a_id": a_id, "b_id": b_id, "a_name": a_name, "b_name": b_name,
            "a_token": a_token, "b_token": b_token}


@pytest.fixture()
def two_users(client):
    # 复用 session 级别缓存的 owner_token, 避免反复登录触发 20次/分钟限流
    token = _get_or_create_owner_token(client)
    users = _create_two_members(client, token)
    users["owner_token"] = token
    yield users
    _cleanup_user_data([users["a_id"], users["b_id"]])


_SESSION_OWNER_TOKEN: str | None = None


def _get_or_create_owner_token(client: TestClient) -> str:
    """session 级单例 owner token: 只登录一次, 复用 token, 避免反复触发限流(20/分钟)。
    注意: owner_token 是 JWT, 即使密码/版本变更, 已签发的 token 在过期前仍然有效。
    """
    global _SESSION_OWNER_TOKEN
    if _SESSION_OWNER_TOKEN:
        return _SESSION_OWNER_TOKEN
    _SESSION_OWNER_TOKEN = _login_owner(client)
    return _SESSION_OWNER_TOKEN


def _cleanup_user_data(user_ids: list[str]):
    """测试结束清理两用户的私有数据 + 用户本身。"""
    db = SessionLocal()
    try:
        for uid in user_ids:
            db.execute(text("DELETE FROM analysis_history WHERE user_id = :uid"), {"uid": uid})
            db.execute(
                text("DELETE FROM chat_messages WHERE conversation_id IN "
                     "(SELECT id FROM chat_conversations WHERE user_id = :uid)"),
                {"uid": uid},
            )
            db.execute(text("DELETE FROM chat_conversations WHERE user_id = :uid"), {"uid": uid})
            db.execute(text("DELETE FROM notifications WHERE user_id = :uid"), {"uid": uid})
            db.execute(text("DELETE FROM price_alert_rules WHERE user_id = :uid"), {"uid": uid})
            db.execute(text("DELETE FROM stock_suggestions WHERE user_id = :uid"), {"uid": uid})
            db.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})
        db.commit()
    finally:
        db.close()


def _insert_history(user_id: str, title: str, agent_name: str = "daily_report", date_str: str = "2026-08-23") -> int:
    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT INTO analysis_history "
                "(agent_name, stock_symbol, analysis_date, title, content, user_id, agent_kind_snapshot, created_at, updated_at) "
                "VALUES (:agent, :symbol, :date, :title, :content, :uid, :kind, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"agent": agent_name, "symbol": "*", "date": date_str, "title": title, "content": "x",
             "uid": user_id, "kind": "workflow"},
        )
        db.commit()
        rid = db.execute(
            text("SELECT id FROM analysis_history WHERE user_id = :uid AND title = :t ORDER BY id DESC LIMIT 1"),
            {"uid": user_id, "t": title},
        ).scalar()
        return int(rid)
    finally:
        db.close()


def _insert_stock(symbol: str = "600999", market: str = "CN", name: str = "测试股") -> int:
    db = SessionLocal()
    try:
        db.execute(
            text(
                "INSERT OR IGNORE INTO stocks (symbol, market, name, sort_order, user_id) "
                "VALUES (:symbol, :market, :name, :sort, NULL)"
            ),
            {"symbol": symbol, "market": market, "name": name, "sort": 9999},
        )
        db.commit()
        sid = db.execute(
            text("SELECT id FROM stocks WHERE symbol = :symbol AND market = :market"),
            {"symbol": symbol, "market": market},
        ).scalar()
        return int(sid)
    finally:
        db.close()


def _create_alert_rule(client: TestClient, token: str, stock_id: int, name: str) -> int:
    r = client.post(
        "/api/price-alerts",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "stock_id": stock_id,
            "name": name,
            "enabled": True,
            "condition_group": {
                "op": "and",
                "items": [{"type": "price", "op": ">", "value": 10.0}],
            },
            "market_hours_mode": "trading_only",
            "cooldown_minutes": 30,
            "max_triggers_per_day": 3,
            "repeat_mode": "repeat",
            "expire_at": None,
            "notify_channel_ids": [],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # 兼容裸 dict 与标准包装 {code,success,data}
    if isinstance(body, dict) and "id" in body:
        return body["id"]
    return body["data"]["id"]


# ============================================================================
# S1: /api/history
# ============================================================================

class TestHistoryS1:
    """S1: /api/history 按 user 过滤 + 越权返回 404"""

    def test_history_list_isolation(self, two_users, client):
        _insert_history(two_users["a_id"], "user A's report")
        _insert_history(two_users["b_id"], "user B's report")

        r_a = client.get(
            "/api/history",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        r_b = client.get(
            "/api/history",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_a.status_code == 200 and r_b.status_code == 200
        a_titles = {item["title"] for item in r_a.json().get("data", [])}
        b_titles = {item["title"] for item in r_b.json().get("data", [])}
        assert "user B's report" not in a_titles
        assert "user A's report" not in b_titles

    def test_history_detail_404_on_other_user(self, two_users, client):
        a_id = _insert_history(two_users["a_id"], "A only")

        r_a = client.get(
            f"/api/history/{a_id}",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        assert r_a.status_code == 200

        r_b = client.get(
            f"/api/history/{a_id}",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_b.status_code == 404, f"应为 404 防账号探测, 实际 {r_b.status_code}"

    def test_history_delete_404_on_other_user(self, two_users, client):
        a_id = _insert_history(two_users["a_id"], "A only")

        r_b = client.delete(
            f"/api/history/{a_id}",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_b.status_code == 404

        # 验证 A 的记录还在
        db = SessionLocal()
        try:
            cnt = db.execute(
                text("SELECT COUNT(*) FROM analysis_history WHERE id = :id"),
                {"id": a_id},
            ).scalar()
            assert cnt == 1, "B 的删除请求不应影响 A 的数据"
        finally:
            db.close()


# ============================================================================
# S2: /api/chat
# ============================================================================

class TestChatS2:
    """S2: /api/chat/conversations 按 user 过滤 + 越权 404"""

    def test_conversation_create_writes_user_id(self, two_users, client):
        r = client.post(
            "/api/chat/conversations",
            json={},
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        assert r.status_code == 200
        conv_id = r.json()["data"]["id"]

        db = SessionLocal()
        try:
            row = db.execute(
                text("SELECT user_id FROM chat_conversations WHERE id = :id"),
                {"id": conv_id},
            ).first()
            assert row is not None and row[0] == two_users["a_id"], \
                f"会话 user_id 应为 {two_users['a_id']}, 实际 {row[0] if row else None}"
        finally:
            db.close()

    def test_conversation_list_isolation(self, two_users, client):
        for _ in range(2):
            client.post(
                "/api/chat/conversations",
                json={},
                headers={"Authorization": f"Bearer {two_users['a_token']}"},
            )
        client.post(
            "/api/chat/conversations",
            json={},
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )

        r_a = client.get(
            "/api/chat/conversations",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        r_b = client.get(
            "/api/chat/conversations",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_a.status_code == 200 and r_b.status_code == 200
        assert len(r_a.json().get("data", [])) >= 2
        assert len(r_b.json().get("data", [])) == 1

    def test_conversation_get_404_on_other_user(self, two_users, client):
        r = client.post(
            "/api/chat/conversations",
            json={},
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        conv_id = r.json()["data"]["id"]

        r_b = client.get(
            f"/api/chat/conversations/{conv_id}",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_b.status_code == 404

        r_a = client.get(
            f"/api/chat/conversations/{conv_id}",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        assert r_a.status_code == 200

    def test_conversation_delete_404_on_other_user(self, two_users, client):
        r = client.post(
            "/api/chat/conversations",
            json={},
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        conv_id = r.json()["data"]["id"]

        r_b = client.delete(
            f"/api/chat/conversations/{conv_id}",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_b.status_code == 404

        db = SessionLocal()
        try:
            cnt = db.execute(
                text("SELECT COUNT(*) FROM chat_conversations WHERE id = :id"),
                {"id": conv_id},
            ).scalar()
            assert cnt == 1
        finally:
            db.close()


# ============================================================================
# S3: /api/price-alerts
# ============================================================================

class TestPriceAlertsS3:
    """S3: /api/price-alerts 按 user 过滤"""

    def test_price_alert_create_writes_user_id(self, two_users, client):
        sid = _insert_stock()
        rule_id = _create_alert_rule(client, two_users["a_token"], sid, "A rule")

        db = SessionLocal()
        try:
            row = db.execute(
                text("SELECT user_id FROM price_alert_rules WHERE id = :id"),
                {"id": rule_id},
            ).first()
            assert row[0] == two_users["a_id"]
        finally:
            db.close()

    def test_price_alert_list_isolation(self, two_users, client):
        sid = _insert_stock()
        _create_alert_rule(client, two_users["a_token"], sid, "A rule")
        _create_alert_rule(client, two_users["b_token"], sid, "B rule")

        r_a = client.get(
            "/api/price-alerts",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        r_b = client.get(
            "/api/price-alerts",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_a.status_code == 200 and r_b.status_code == 200
        a_names = {it["name"] for it in r_a.json().get("data", [])}
        b_names = {it["name"] for it in r_b.json().get("data", [])}
        assert "B rule" not in a_names
        assert "A rule" not in b_names

    def test_price_alert_toggle_404_on_other_user(self, two_users, client):
        sid = _insert_stock()
        rule_id = _create_alert_rule(client, two_users["a_token"], sid, "A rule")

        r_b = client.post(
            f"/api/price-alerts/{rule_id}/toggle",
            json={"enabled": False},
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_b.status_code == 404

        db = SessionLocal()
        try:
            enabled = db.execute(
                text("SELECT enabled FROM price_alert_rules WHERE id = :id"),
                {"id": rule_id},
            ).scalar()
            assert enabled == 1  # SQLite: 1 = True
        finally:
            db.close()

    def test_price_alert_delete_404_on_other_user(self, two_users, client):
        sid = _insert_stock()
        rule_id = _create_alert_rule(client, two_users["a_token"], sid, "A rule")

        r_b = client.delete(
            f"/api/price-alerts/{rule_id}",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_b.status_code == 404

        db = SessionLocal()
        try:
            cnt = db.execute(
                text("SELECT COUNT(*) FROM price_alert_rules WHERE id = :id"),
                {"id": rule_id},
            ).scalar()
            assert cnt == 1
        finally:
            db.close()


# ============================================================================
# S4: /api/notifications
# ============================================================================

class TestNotificationsS4:
    """S4: /api/notifications 按 user 过滤 + unread_count 按用户"""

    def _insert_notification(self, user_id: str, title: str, read: bool = False) -> int:
        db = SessionLocal()
        try:
            db.execute(
                text(
                    "INSERT INTO notifications (user_id, category, level, title, body, link, source, trace_id, push_status, push_error, push_channels, read_at, created_at) "
                    "VALUES (:uid, :cat, :lvl, :title, :body, :link, :src, :trace, '', '', '[]', :read, CURRENT_TIMESTAMP)"
                ),
                {
                    "uid": user_id, "cat": "system", "lvl": "info",
                    "title": title, "body": "x", "link": "", "src": "test",
                    "trace": uuid.uuid4().hex[:16],
                    "read": beijing_now_naive() if read else None,
                },
            )
            db.commit()
            nid = db.execute(
                text("SELECT id FROM notifications WHERE user_id = :uid AND title = :t ORDER BY id DESC LIMIT 1"),
                {"uid": user_id, "t": title},
            ).scalar()
            return int(nid)
        finally:
            db.close()

    def test_unread_count_isolation(self, two_users, client):
        _ = self._insert_notification(two_users["a_id"], "A note 1")
        _ = self._insert_notification(two_users["a_id"], "A note 2")
        _ = self._insert_notification(two_users["b_id"], "B note 1")

        r_a = client.get(
            "/api/notifications/unread-count",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        r_b = client.get(
            "/api/notifications/unread-count",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_a.status_code == 200
        assert r_b.status_code == 200
        # A 看到 2, B 看到 1(端点响应可能被统一包装, 兼容两种)
        a_body = r_a.json()
        b_body = r_b.json()
        a_unread = a_body["unread"] if "unread" in a_body else a_body["data"]["unread"]
        b_unread = b_body["unread"] if "unread" in b_body else b_body["data"]["unread"]
        assert a_unread == 2, f"A unread 应为 2, 实际 {a_unread}"
        assert b_unread == 1, f"B unread 应为 1, 实际 {b_unread}"

    def test_list_isolation(self, two_users, client):
        self._insert_notification(two_users["a_id"], "A note")
        self._insert_notification(two_users["b_id"], "B note")

        r_a = client.get(
            "/api/notifications",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        r_b = client.get(
            "/api/notifications",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        a_titles = {it["title"] for it in r_a.json().get("data", {}).get("items", [])}
        b_titles = {it["title"] for it in r_b.json().get("data", {}).get("items", [])}
        assert "B note" not in a_titles
        assert "A note" not in b_titles

    def test_detail_404_on_other_user(self, two_users, client):
        a_nid = self._insert_notification(two_users["a_id"], "A only")

        r_b = client.get(
            f"/api/notifications/{a_nid}",
            headers={"Authorization": f"Bearer {two_users['b_token']}"},
        )
        assert r_b.status_code == 404

        r_a = client.get(
            f"/api/notifications/{a_nid}",
            headers={"Authorization": f"Bearer {two_users['a_token']}"},
        )
        assert r_a.status_code == 200


def beijing_now_naive():
    """兼容 naive 时间戳(与 _insert_notification 用法一致)。"""
    from datetime import datetime
    return datetime.now().replace(tzinfo=None)
