"""设备限制回归(2026-10-23 P0): 会话 id 唯一 / 超限踢最早 / 被踢 token 立刻 401。

修复前的真实故障链:
1. `record_session(session_id=token[:32])` —— JWT 头部恒为 `{"alg":"HS256","typ":"JWT"}`,
   Base64 后前 32 字符全平台完全相同 → 所有用户所有设备覆写同一行;
2. `enforce_device_limit` 的 rows 永远 ≤1, 踢人循环永不进入;
3. `get_current_user` 根本不查 `UserSession` → 即使删行, 旧 JWT 12h 内照样可用。

本文件钉住四条不可回退的性质:
(a) 两次登录产生两个不同 session_id, 且都等于 JWT 的 jti;
(b) 超过上限时最早的会话行被删;
(c) 会话表查询抛异常时鉴权仍放行(fail-open, 绝不因会话表故障全站 401);
(d) 被踢会话的 token 再请求得 401。
"""
import pytest
from fastapi.testclient import TestClient

from src.web.database import SessionLocal
from src.web.api.auth import create_user

U1 = "sess-user-1023"
U2 = "sess-other-1023"
PW = "sess-pw-1023-a"


@pytest.fixture()
def client(monkeypatch):
    # 登录限流在测试进程内跨文件累计 → 合跑会 429, 单测关闭
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    # 上限固定 2 台, 不依赖 free_tier 配置(默认也是 2, 这里显式钉住)
    monkeypatch.setattr("src.core.permissions.effective_max_sessions", lambda db=None: 2)

    db = SessionLocal()
    try:
        from tests.conftest import purge_users

        purge_users(db, only_username=U1)
        purge_users(db, only_username=U2)
        db.commit()
        create_user(db, U1, PW, "member", email="sess1-1023@example.com")
        create_user(db, U2, PW, "member", email="sess2-1023@example.com")
    finally:
        db.close()

    from src.web.app import app

    return TestClient(app)


def _login(client, username=U1, password=PW, ua="Mozilla/5.0 (Windows NT 10.0) Chrome/120.0"):
    r = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={"User-Agent": ua},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _me(client, token):
    return client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})


def _sessions(client, token):
    r = client.get("/api/auth/sessions", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _rows(username=U1):
    from src.db.models import User, UserSession

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        return (
            db.query(UserSession)
            .filter(UserSession.user_id == user.id)
            .order_by(UserSession.last_seen.asc())
            .all()
        )
    finally:
        db.close()


# ── (a) 会话 id 唯一 ───────────────────────────────────────────────────


def test_two_logins_get_distinct_session_ids(client):
    t1 = _login(client, ua="Mozilla/5.0 (Windows NT 10.0) Chrome/120.0")
    t2 = _login(client, ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1")

    # 修复前: 两行会被压成同一行(token[:32] 相同), 这里必须各自成行
    ids = [r.session_id for r in _rows()]
    assert len(ids) == 2, f"两次登录应有两个会话行, 实际 {ids}"
    assert len(set(ids)) == 2
    # 且都不是 JWT 头部那 32 个恒定字符
    assert t1[:32] == t2[:32], "前提: JWT 头部恒定(本 bug 的根因)"
    assert all(sid != t1[:32] for sid in ids)

    # session_id 必须是 jti(可从 token 里取回对照)
    from src.core.auth_tokens import decode_token, session_id_from_payload

    jti1 = session_id_from_payload(decode_token(t1))
    jti2 = session_id_from_payload(decode_token(t2))
    assert jti1 and jti2 and jti1 != jti2
    assert set(ids) == {jti1, jti2}


def test_sessions_endpoint_lists_devices_without_leaking_token(client):
    token = _login(client)
    data = _sessions(client, token)

    assert data["limit"] == 2
    assert len(data["sessions"]) == 1
    s = data["sessions"][0]
    assert s["is_current"] is True
    assert s["device"] == "Chrome · Windows"
    assert s["id_prefix"] == s["id"][:8]
    assert len(s["id_prefix"]) == 8
    # 列表只给 session 标识, 绝不能回 token 明文
    assert token not in str(data)
    assert token[:24] not in str(data)


def test_device_label_parsing():
    from src.web.api.auth import _parse_device_label

    assert _parse_device_label(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605.1.15 Safari/604.1"
    ).startswith("Safari")
    assert "iOS" in _parse_device_label("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604.1")
    assert _parse_device_label(
        "Mozilla/5.0 (Windows NT 10.0) Chrome/120.0 Safari/537.36 Edg/120.0"
    ) == "Edge · Windows"
    assert _parse_device_label(
        "Mozilla/5.0 (X11; Linux x86_64) Chrome/119.0 Safari/537.36"
    ) == "Chrome · Linux"
    assert "MicroMessenger" not in _parse_device_label(
        "Mozilla/5.0 (Linux; Android 13) MicroMessenger/8.0"
    )
    assert _parse_device_label("Mozilla/5.0 (Linux; Android 13) MicroMessenger/8.0").startswith("微信")
    assert _parse_device_label("curl/8.4.0") == "curl · 未知系统"
    assert _parse_device_label("") == "未知设备"


# ── (b) 超限踢最早 ─────────────────────────────────────────────────────


def test_device_limit_kicks_oldest_session(client):
    t1 = _login(client, ua="Mozilla/5.0 (Windows NT 10.0) Chrome/120.0")
    oldest_sid = _rows()[0].session_id

    t2 = _login(client, ua="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120.0")
    t3 = _login(client, ua="Mozilla/5.0 (X11; Linux x86_64) Firefox/121.0")

    rows = _rows()
    assert len(rows) == 2, f"上限 2 台, 应剩 2 行, 实际 {len(rows)}"
    remaining = {r.session_id for r in rows}
    assert oldest_sid not in remaining, "最早会话行必须被删除"

    # 最新的两台仍可用
    assert _me(client, t2).status_code == 200
    assert _me(client, t3).status_code == 200
    # (d) 被踢那台的 token 必须立刻 401(不靠 12h 自然过期)
    assert _me(client, t1).status_code == 401


# ── (c) fail-open ──────────────────────────────────────────────────────


def test_current_user_fail_open_when_session_check_raises(client, monkeypatch):
    """会话表查询抛异常时 get_current_user 仍放行 —— 绝不因会话表故障全站 401。"""
    token = _login(client)

    def _boom(*args, **kwargs):
        raise RuntimeError("user_sessions 表不可用")

    monkeypatch.setattr("src.core.permissions.session_is_active", _boom)

    r = _me(client, token)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["user"]["username"] == U1


def test_session_is_active_fail_open_on_broken_db():
    """core 层直接验证: 连接/查询炸掉 → True(放行), 而不是抛错或 False。"""
    from src.core.permissions import session_is_active

    class _BrokenDB:
        def execute(self, *a, **k):
            raise RuntimeError("db down")

        def query(self, *a, **k):
            raise RuntimeError("db down")

    assert session_is_active(_BrokenDB(), "some-jti", "some-uid") is True
    # 无 jti 的老 token 也必须放行
    assert session_is_active(_BrokenDB(), None) is True


# ── (d) 踢单台 / 踢其它 ────────────────────────────────────────────────


def test_kick_single_session_returns_404_for_foreign_session(client):
    _login(client)  # U1
    other = _login(client, username=U2)  # U2 的会话

    mine = _login(client)
    other_sid = _sessions(client, other)["sessions"][0]["id"]

    r = client.delete(
        f"/api/auth/sessions/{other_sid}", headers={"Authorization": f"Bearer {mine}"}
    )
    assert r.status_code == 404, "越权踢别人的会话必须 404(不泄露存在性)"
    # 别人的会话不受影响
    assert _me(client, other).status_code == 200


def test_kick_others_keeps_current_device(client):
    t1 = _login(client)
    t2 = _login(client)

    r = client.post("/api/auth/sessions/kick-others", headers={"Authorization": f"Bearer {t2}"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["kicked"] == 1

    assert _me(client, t2).status_code == 200
    assert _me(client, t1).status_code == 401
    assert len(_rows()) == 1


def test_kick_self_marks_current_flag(client):
    token = _login(client)
    sid = _sessions(client, token)["sessions"][0]["id"]

    r = client.delete(f"/api/auth/sessions/{sid}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["current"] is True
    assert _me(client, token).status_code == 401


def test_kicked_session_cannot_connect_ws(client):
    """WS 与 HTTP 同口径: 被踢会话的 token 不得再通过 WS 握手校验。"""
    from src.core.auth_tokens import decode_token
    from src.web.api.auth import verify_ws_token_payload

    t1 = _login(client)
    token2 = _login(client)  # 触发上限(2), 但 t1 仍应存活
    assert _me(client, t1).status_code == 200

    client.post("/api/auth/sessions/kick-others", headers={"Authorization": f"Bearer {token2}"})
    # 先确认 HTTP 已拒
    assert _me(client, t1).status_code == 401

    db = SessionLocal()
    try:
        assert verify_ws_token_payload(db, decode_token(t1)) is None
        assert verify_ws_token_payload(db, decode_token(token2)) is not None
    finally:
        db.close()
