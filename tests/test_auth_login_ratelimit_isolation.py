"""登录失败限流跨用例隔离回归(2026-10-11 CI shard 1 顺序污染)。

背景: `src/core/login_ratelimit` 的失败计数是**进程级模块状态**(key = ip|username)。
用例 A 故意用错口令登录(或 admin 因残留 owner 登录 401)累计的失败计数会留给
用例 B ⇒ 用例 B 的正常登录撞 429「登录失败次数过多,请 10 分钟后重试」——合跑红、单跑绿。

本文件用两个相邻用例钉死隔离:
  ① test_..._lock_the_counter: 连续 5 次错口令, 打满并锁定(第 6 次同用户名直接 429)——
     这是产品限流语义, 必须**真实存在**(断言强度不减, 不跳过);
  ② test_..._not_polluted: 紧接其后的正确口令登录**不得**被①的锁定连坐 429。

② 能绿的前提是 conftest 的 `_reset_login_ratelimit` 每个用例前清零 ——
把该 fixture 临时注释掉, ② 必红(429)。这就是本修复的反向验证锚点。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

ADMIN_USER = "admin"
ADMIN_PW = "iso-ratelimit-0606"


@pytest.fixture()
def client(monkeypatch):
    # 只关中间件 20/min/IP 敏感路径限流(它是另一套桶); 登录失败锁定(login_ratelimit)
    # 本文件要**真实生效**才能验隔离。
    monkeypatch.setattr("src.web.middleware.RATE_LIMIT_ENABLED", False)
    monkeypatch.setenv("AUTH_USERNAME", ADMIN_USER)
    monkeypatch.setenv("AUTH_PASSWORD", ADMIN_PW)

    from src.web.database import SessionLocal
    from src.web.models import User

    db = SessionLocal()
    try:
        from tests.conftest import purge_users

        # 所有 owner(残留 owner 会让 get_or_create_owner 短路) + 名为 admin 的账号
        # 一并清掉再按 env 重建, 保证本文件不依赖上游跑过什么(自洽化)。
        stale_ids = {str(u.id) for u in db.query(User).filter(User.role == "owner").all()}
        stale_ids |= {str(u.id) for u in db.query(User).filter(User.username == ADMIN_USER).all()}
        if stale_ids:
            purge_users(db, ids=sorted(stale_ids))
        from src.web.api.auth import get_or_create_owner

        get_or_create_owner(db)
    finally:
        db.close()

    from src.web.app import app

    return TestClient(app)


def _login(client: TestClient, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def test_a_five_wrong_passwords_lock_the_counter(client):
    """连续 5 次错口令 → 计数打满并锁定; 第 6 次同用户名直接 429(产品限流语义)。"""
    for i in range(5):
        r = _login(client, ADMIN_USER, "definitely-wrong-pw")
        assert r.status_code == 401, f"第 {i + 1} 次错口令应 401, 实际 {r.status_code}: {r.text}"
    r = _login(client, ADMIN_USER, "definitely-wrong-pw")
    assert r.status_code == 429, f"第 6 次应被限流 429, 实际 {r.status_code}: {r.text}"


def test_b_correct_login_not_polluted_by_previous_test(client):
    """紧接上例的正确口令登录**不得**被上例累计的失败计数连坐 429。

    conftest `_reset_login_ratelimit` 每例前清零 ⇒ 本例 200;
    fixture 被注释掉时本例必 429(反向验证)。
    """
    r = _login(client, ADMIN_USER, ADMIN_PW)
    assert r.status_code == 200, f"正确口令登录被跨用例失败计数连坐: {r.status_code} {r.text}"
    assert (r.json().get("data") or {}).get("token")
