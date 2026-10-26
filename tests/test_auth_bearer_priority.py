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
    """引导 owner(env 凭据), 返回一个 TestClient(自带 Cookie jar)。

    2026-10-01 自洽化(CI shard 3 确定性红的根因修复): 本用例只依赖**自己**摆好的
    身份状态, 不依赖同 shard 上游测试跑过什么。

    旧写法只 `purge_users(only_username="admin")` 就返回, 隐含假定"库里的 owner
    只有 admin"。但登录端点是 `get_or_create_owner()` —— 只要库中**存在任意一个
    owner** 就直接返回它。上游若有测试写了一个 owner 且**不清理**(如
    `tests/test_invite_codes.py` 的 `iv_test_owner_v1`), 本夹具把 admin 删掉后
    owner 仍在 ⇒ `get_or_create_owner` 命中残留 owner **短路**, 不再按 env 重建
    admin ⇒ `admin` 登录 401「用户名/邮箱或密码错误」(3 例全红)。

    修法: 先把**所有 owner**(含 admin 本身)清干净, 再**显式**按 env 重建 owner;
    无论前面跑过什么, 本夹具都把库收敛到同一初始态, 且不靠"import app 时的惰性
    引导"这种隐式时序。断言强度不变 —— 仍在真实验 Bearer 优先/不回退 Cookie。
    """
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    monkeypatch.setenv("AUTH_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AUTH_PASSWORD", ADMIN_PW)

    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        from tests.conftest import purge_users

        # 所有 owner(残留 owner 会让 get_or_create_owner 短路) + 名为 admin 的账号
        # (可能残留旧口令哈希) 一并清掉, 再重建。
        stale_ids = {str(u.id) for u in db.query(User).filter(User.role == "owner").all()}
        stale_ids |= {str(u.id) for u in db.query(User).filter(User.username == ADMIN_USER).all()}
        if stale_ids:
            purge_users(db, ids=sorted(stale_ids))

        # 显式重建 owner(env 凭据); 不再依赖 import app 的惰性引导副作用。
        from src.web.api.auth import get_or_create_owner

        get_or_create_owner(db)
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
