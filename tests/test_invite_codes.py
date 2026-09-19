# -*- coding: utf-8 -*-
"""邀请码注册：三条验收线的钉子（2026-09-19 内部使用模式）。

用户拍板"暂时内部使用"，注册改**邀请制**。合规上真正的红线是"面向公众"——
公开自助注册的口子开着，形式上就是面向公众提供分析建议，所以门必须收在管理员手里。

本文件钉住的**三条硬验收线**：
  1. **无邀请码注册被拒**（无码/错码/停用/过期/用尽，且原因可区分）；
  2. **一个码用满即失效**（含**并发不超发**——核销必须是单条条件 UPDATE）；
  3. **审计落库**（`invite_code_uses` 流水 + audit 日志，且**日志不留明文码**）。

⚠️ 口令一律运行时随机生成，不写字面量 —— 字面口令会被 CI 的 gitleaks 规则拦死发版。
"""
from __future__ import annotations

import os
import secrets
import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from src.web.database import SessionLocal, init_db

_PW = "IV-" + secrets.token_urlsafe(18)
_MEMBER_PW = "IVM-" + secrets.token_urlsafe(18)
_OWNER = "iv_test_owner_v1"
_MEMBER = "iv_test_member_v1"


@pytest.fixture(scope="module", autouse=True)
def _ensure_db():
    os.environ.setdefault("SKILL_KEY_SALT", "test-salt-invite")
    init_db()
    yield
    # 清理本模块产生的数据（不污染持久化测试库）
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM invite_code_uses WHERE username LIKE 'iv\\_%' ESCAPE '\\'"))
        db.execute(text("DELETE FROM invite_codes WHERE created_by = :o OR note LIKE 'IVTEST%'"), {"o": _OWNER})
        db.execute(text("DELETE FROM app_settings WHERE key = 'register_mode'"))
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def client():
    from src.web.app import app

    return TestClient(app)


def _set_mode(db, mode: str | None) -> None:
    """把注册模式钉到确定状态：先清 DB 行与 env，再按需写 DB 行。"""
    db.execute(text("DELETE FROM app_settings WHERE key = 'register_mode'"))
    db.commit()
    os.environ.pop("REGISTER_MODE", None)
    if mode is not None:
        db.execute(
            text("INSERT INTO app_settings (key, value, description) VALUES ('register_mode', :v, '')"),
            {"v": mode},
        )
        db.commit()


def _stub_email_code(email: str) -> str:
    """直接把注册验证码塞进内存 store（绕开真实发信）。"""
    from src.web.api.email_verify import EmailCode, _lock, _store, _store_key

    code = f"{secrets.randbelow(1000000):06d}"
    key = email.lower().strip()
    with _lock:
        _store[_store_key(key, "register")] = EmailCode(
            code=code, email=key, purpose="register", created_at=time.time(), used=False
        )
    return code


def _register(client, email: str, invite_code: str | None = None, username: str | None = None):
    code = _stub_email_code(email)
    payload = {"email": email, "password": _PW, "code": code}
    if username:
        payload["username"] = username
    if invite_code is not None:
        payload["invite_code"] = invite_code
    return client.post("/api/auth/register", json=payload)


def _mk_code(max_uses: int = 1, expires_in_days: int | None = None, note: str = "IVTEST") -> str:
    from src.core.invite_codes import create_invite_code

    db = SessionLocal()
    try:
        return create_invite_code(db, created_by=_OWNER, note=note, max_uses=max_uses,
                                  expires_in_days=expires_in_days)["code"]
    finally:
        db.close()


def _login(client, ident: str, pw: str) -> str:
    r = client.post("/api/auth/login", json={"username": ident, "password": pw})
    assert r.status_code == 200, r.text
    body = r.json()
    return (body.get("data") or {}).get("token") or ""


def _ensure_member(client) -> str:
    """建一个普通成员（用于验权限），返回其 token。"""
    db = SessionLocal()
    try:
        from src.web.api.auth import create_user
        from src.web.models import User

        u = db.query(User).filter(User.username == _MEMBER).first()
        if not u:
            create_user(db, _MEMBER, _MEMBER_PW, "member", email=f"{_MEMBER}@example.com")
    finally:
        db.close()
    return _login(client, _MEMBER, _MEMBER_PW)


def _ensure_owner(client) -> str:
    db = SessionLocal()
    try:
        from src.web.api.auth import create_user
        from src.web.models import User

        u = db.query(User).filter(User.username == _OWNER).first()
        if not u:
            create_user(db, _OWNER, _PW, "owner", email=f"{_OWNER}@example.com")
    finally:
        db.close()
    return _login(client, _OWNER, _PW)


# ── 验收线 1：无邀请码注册被拒 ───────────────────────────────────────────

def test_default_mode_is_invite():
    """默认必须是 invite —— 内部使用模式不允许"忘了配就变公开"。"""
    db = SessionLocal()
    try:
        _set_mode(db, None)
        from src.core.invite_codes import DEFAULT_MODE, get_register_mode

        assert DEFAULT_MODE == "invite"
        assert get_register_mode(db) == "invite"
    finally:
        db.close()


@pytest.mark.parametrize(
    "invite_code, expect_word",
    [
        (None, "请填写邀请码"),
        ("", "请填写邀请码"),
        ("ZZZZZZZZ", "邀请码无效"),
    ],
)
def test_register_without_valid_code_rejected(client, invite_code, expect_word):
    db = SessionLocal()
    try:
        _set_mode(db, "invite")
    finally:
        db.close()
    r = _register(client, f"iv_nocode_{secrets.token_hex(3)}@example.com", invite_code)
    assert r.status_code == 400, r.text
    assert expect_word in r.text, r.text


def test_disabled_and_expired_codes_rejected(client):
    """停用/过期各有各的文案（不能一律"邀请码无效"）。"""
    db = SessionLocal()
    try:
        _set_mode(db, "invite")
    finally:
        db.close()

    # 停用
    from src.core.invite_codes import disable_invite_code

    c1 = _mk_code(note="IVTEST-disable")
    db = SessionLocal()
    try:
        disable_invite_code(db, c1)
    finally:
        db.close()
    r = _register(client, f"iv_dis_{secrets.token_hex(3)}@example.com", c1)
    assert r.status_code == 400 and "停用" in r.text, r.text

    # 过期（直接改库到过去时间）
    c2 = _mk_code(note="IVTEST-expire")
    db = SessionLocal()
    try:
        db.execute(text("UPDATE invite_codes SET expires_at = :t WHERE code = :c"),
                   {"t": "2000-01-01 00:00:00", "c": c2})
        db.commit()
    finally:
        db.close()
    r = _register(client, f"iv_exp_{secrets.token_hex(3)}@example.com", c2)
    assert r.status_code == 400 and "过期" in r.text, r.text


def test_mode_open_still_works_without_code(client):
    """兼容历史行为：open 模式下不需要邀请码（现有注册链路测试靠它）。"""
    db = SessionLocal()
    try:
        _set_mode(db, "open")
    finally:
        db.close()
    r = _register(client, f"iv_open_{secrets.token_hex(3)}@example.com")
    assert r.status_code == 200, r.text


def test_mode_closed_rejects_all(client):
    db = SessionLocal()
    try:
        _set_mode(db, "closed")
    finally:
        db.close()
    r = _register(client, f"iv_closed_{secrets.token_hex(3)}@example.com")
    assert r.status_code == 403, r.text


# ── 验收线 2：一个码用满即失效 ───────────────────────────────────────────

def test_code_exhausted_after_max_uses(client):
    db = SessionLocal()
    try:
        _set_mode(db, "invite")
    finally:
        db.close()
    code = _mk_code(max_uses=1)
    r1 = _register(client, f"iv_ok_{secrets.token_hex(3)}@example.com", code)
    assert r1.status_code == 200, r1.text
    r2 = _register(client, f"iv_again_{secrets.token_hex(3)}@example.com", code)
    assert r2.status_code == 400 and "用尽" in r2.text, r2.text


def test_multi_use_code_allows_exact_uses(client):
    """max_uses=2 → 第 3 次必须失败（不是"少一次/多一次"）。"""
    db = SessionLocal()
    try:
        _set_mode(db, "invite")
    finally:
        db.close()
    code = _mk_code(max_uses=2)
    ok = [_register(client, f"iv_m{i}_{secrets.token_hex(3)}@example.com", code).status_code for i in range(3)]
    assert ok[:2] == [200, 200], ok
    assert ok[2] == 400, ok


def test_concurrent_redeem_does_not_over_issue():
    """**并发不超发**：20 个线程抢同一个 max_uses=1 的码，只能有 1 个成功。

    这条是核销实现的核心正确性 —— 若用"先查后改"，并发下会多个线程同时通过检查。
    """
    from src.core.invite_codes import InviteCodeError, create_invite_code, redeem

    db = SessionLocal()
    try:
        code = create_invite_code(db, created_by=_OWNER, note="IVTEST-concurrent", max_uses=1)["code"]
    finally:
        db.close()

    ok, failed = [], []
    lock = threading.Lock()

    def worker(i: int):
        s = SessionLocal()
        try:
            redeem(s, code, username=f"iv_c{i}", client_ip=f"10.0.0.{i}")
            with lock:
                ok.append(i)
        except InviteCodeError as e:
            with lock:
                failed.append(e.reason)
        except Exception as e:  # noqa: BLE001
            with lock:
                failed.append(f"err:{type(e).__name__}")
        finally:
            s.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(ok) == 1, f"超发了！成功 {len(ok)} 次: {ok}"
    assert len(failed) == 19


# ── 验收线 3：审计落库 ───────────────────────────────────────────────────

def test_register_writes_invite_audit_and_masked_log(client):
    db = SessionLocal()
    try:
        _set_mode(db, "invite")
    finally:
        db.close()
    code = _mk_code(max_uses=1, note="IVTEST-audit")
    email = f"iv_audit_{secrets.token_hex(3)}@example.com"
    assert _register(client, email, code).status_code == 200

    db = SessionLocal()
    try:
        rows = db.execute(
            text("SELECT code, username, client_ip, used_at FROM invite_code_uses WHERE code = :c"),
            {"c": code},
        ).mappings().all()
        assert len(rows) == 1, "核销没有落审计流水"
        assert rows[0]["used_at"], "审计缺时间戳"
        assert rows[0]["username"], "审计缺使用人"

        # audit 日志里必须**有邀请码痕迹但不能是明文**
        aud = db.execute(
            text("SELECT detail FROM audit_logs WHERE action = 'register' ORDER BY id DESC LIMIT 5")
        ).fetchall()
        details = " ".join(str(r[0]) for r in aud)
        assert "邀请码" in details, f"注册审计没记录邀请码: {details[-160:]}"
        assert code not in details, "审计里出现了完整邀请码明文"
        assert code[-4:] in details, "审计里应保留后 4 位便于追溯"
    finally:
        db.close()


def test_mask_code_hides_all_but_tail():
    from src.core.invite_codes import mask_code

    assert mask_code("ABCD2345") == "****2345"
    assert "ABCD" not in mask_code("ABCD2345")


# ── 管理端点权限：仅 owner ───────────────────────────────────────────────

def test_admin_endpoints_owner_only(client):
    member_tok = _ensure_member(client)
    owner_tok = _ensure_owner(client)

    r = client.get("/api/admin/invite-codes", headers={"Authorization": f"Bearer {member_tok}"})
    assert r.status_code in (401, 403), f"普通用户竟然能看邀请码列表: {r.status_code}"

    r = client.post(
        "/api/admin/invite-codes",
        json={"note": "IVTEST-owner", "max_uses": 1},
        headers={"Authorization": f"Bearer {member_tok}"},
    )
    assert r.status_code in (401, 403), f"普通用户竟然能生成邀请码: {r.status_code}"

    r = client.post(
        "/api/admin/invite-codes",
        json={"note": "IVTEST-owner", "max_uses": 1},
        headers={"Authorization": f"Bearer {owner_tok}"},
    )
    assert r.status_code == 200, r.text
    created = (r.json().get("data") or r.json()).get("code")
    assert created and len(created) == 8

    # 列表里能查到，且能停用
    r = client.get("/api/admin/invite-codes", headers={"Authorization": f"Bearer {owner_tok}"})
    assert r.status_code == 200
    items = (r.json().get("data") or r.json()).get("items") or []
    assert any(i["code"] == created for i in items), "生成的码不在列表里"

    r = client.post(f"/api/admin/invite-codes/{created}/disable",
                    headers={"Authorization": f"Bearer {owner_tok}"})
    assert r.status_code == 200, r.text


def test_mode_endpoint_reflects_setting(client):
    db = SessionLocal()
    try:
        _set_mode(db, "invite")
    finally:
        db.close()
    owner_tok = _ensure_owner(client)
    r = client.get("/api/admin/invite-codes/mode", headers={"Authorization": f"Bearer {owner_tok}"})
    assert r.status_code == 200, r.text
    body = r.json().get("data") or r.json()
    assert body["mode"] == "invite"
