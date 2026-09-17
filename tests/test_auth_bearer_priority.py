"""2026-09-18 决策回归: token 来源裁决 = **Authorization Bearer 优先 → Cookie 兜底**。

背景(原口径 Cookie 优先, 已按老板拍板改掉):
  登录会下发 httpOnly Cookie `sida_token`, 而 `get_current_user` 原先**Cookie 优先**于
  Authorization 头 ⇒ 浏览器里只要残留上一个账号的 Cookie, 显式带了 Bearer 的请求会被按
  **另一个用户**执行(身份静默错位; 本仓表现为测试里 owner 的 PATCH 被当成 member → 403)。

新口径(本文件钉死):
  ① 有 Authorization header ⇒ 它就是权威身份, 且**验不过直接 401, 不回退 Cookie**
     (显式带错 token 却以别人身份通过, 比重拒更危险);
  ② 没有 header ⇒ 走 Cookie(浏览器默认路径, 前端零改动);
  ③ 两个都没有 ⇒ 401;
  ④ 中间件/审计侧与 HTTP 依赖**同源**(都走 `auth.token_from_request`), 不各写一份优先级。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN_USER = "admin"
ADMIN_PW = "admin-test-0606"


@pytest.fixture()
def client(monkeypatch):
    """引导 owner(env 凭据), 返回一个 TestClient(自带 Cookie jar)。"""
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    monkeypatch.setenv("AUTH_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AUTH_PASSWORD", ADMIN_PW)

    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        db.query(User).filter(User.username == ADMIN_USER).delete()
        db.commit()
    finally:
        db.close()

    from src.web.app import app

    return TestClient(app)


def _login(client, username, password):  # noqa: ANN001
    client.cookies.clear()
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["data"]["token"]


def _mk_member(client, token, username="bob"):  # noqa: ANN001
    r = client.post(
        "/api/auth/users",
        headers={"Authorization": f"Bearer {token}"},
        json={"username": username, "password": "bob12345678", "role": "member"},
    )
    assert r.status_code == 200, r.text
    return r.json()["data"]["user"]


# ── ① Bearer 优先 ────────────────────────────────────────────────────────────

def test_bearer_wins_over_cookie(client):
    """owner 的 Bearer + member 的 Cookie → 必须按 **owner** 执行(旧口径会按 member → 403)。"""
    owner_tok = _login(client, ADMIN_USER, ADMIN_PW)
    bob = _mk_member(client, owner_tok)
    bob_tok = _login(client, "bob", "bob12345678")
    assert bob_tok  # 登录后 Cookie jar 里现在是 bob 的 sida_token

    r = client.patch(
        f"/api/auth/users/{bob['id']}",
        headers={"Authorization": f"Bearer {owner_tok}"},  # 显式 Bearer = owner
        json={"is_active": False},
    )
    assert r.status_code == 200, f"Bearer 未能覆盖 Cookie(身份错位): {r.text}"
    assert r.json()["data"]["user"]["is_active"] is False


def test_cookie_alone_still_works(client):
    """没有 Authorization 头时, Cookie 仍是有效通道(前端浏览器默认路径)。"""
    _login(client, ADMIN_USER, ADMIN_PW)  # 只留 Cookie
    r = client.get("/api/portfolio/summary")
    # /api/portfolio/summary 需要登录; 只要不是 401 即证明 Cookie 通道生效
    assert r.status_code != 401, r.text


# ── ② 显式 Bearer 失效 → 401, 不回退 Cookie ─────────────────────────────────

def test_invalid_bearer_does_not_fall_back_to_cookie(client):
    """带了无效 Bearer + 有效 Cookie → 401(**不**用 Cookie 顶上, 否则"带错 token 反而过关")。"""
    _login(client, ADMIN_USER, ADMIN_PW)  # Cookie 有效
    r = client.get(
        "/api/portfolio/summary",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert r.status_code == 401, f"无效 Bearer 竟然被 Cookie 兜住: {r.status_code} {r.text}"


# ── ③ 无凭据 → 401 ──────────────────────────────────────────────────────────

def test_no_credentials_is_401(client):
    from src.web.app import app

    with TestClient(app) as fresh:  # 全新 client: 无 Cookie
        r = fresh.get("/api/portfolio/summary")
    assert r.status_code == 401


# ── ④ 单一真源: 四处读取点都用同一个裁决函数 ─────────────────────────────────

def test_token_resolution_has_single_source_of_truth():
    """中间件/审计侧不得再自己写一份"Cookie 优先"的优先级(否则审计归属会与实际执行身份分叉)。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for rel in ("src/web/middleware.py", "src/web/app.py", "src/web/api/settings.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "token_from_request" in text, f"{rel} 未走同源裁决"
        # 旧写法: 先读 Cookie 再兜 Bearer
        assert 'cookies.get(AUTH_COOKIE_NAME) or ""' not in text, f"{rel} 仍保留 Cookie 优先的旧写法"
