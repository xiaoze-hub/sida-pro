"""钉子: **登录前**要用的端点不许被 CSRF 拦住(2026-09-19 生产实踩)。

## 事故
邮箱注册的第一步 `POST /api/auth/send-code` **没有**在 CSRF 豁免名单里, 而匿名浏览器此时
还没有 `csrf_token` cookie → 中间件返回 403「CSRF token 缺失, 请重新登录」。
用户侧看到的现象就是"点了发送验证码没反应/报错", **邮箱注册整条流程在生产上走不通**
(2026-09-16 上线, 2026-09-19 用户报障才发现)。

## 为什么这类 bug 容易漏
它不在业务代码里, 而在**中间件的豁免名单**里 —— 单测业务函数全绿, 只有"真发一次请求"才暴露。
所以这里按**端点契约**钉: 凡"登录前就要用"的 POST 端点, 必须在豁免名单里。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.web.app import app
from src.web.middleware import CSRF_EXEMPT_PREFIXES

#: 登录前(尚无 session/CSRF cookie)就必须可用的端点 —— 漏一个就是一条断掉的注册/登录路径
PRE_AUTH_POST_ENDPOINTS = (
    "/api/auth/send-code",     # 邮箱验证码(注册/验证码登录第一步)
    "/api/auth/register",      # 自助注册
    "/api/auth/login",         # 密码登录
    "/api/auth/login-by-email",  # 验证码登录(前缀命中 /api/auth/login)
    "/api/auth/logout",        # 清 Cookie 的幂等端点
)


@pytest.mark.parametrize("path", PRE_AUTH_POST_ENDPOINTS)
def test_pre_auth_endpoint_is_csrf_exempt(path: str):
    assert any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES), (
        f"{path} 未在 CSRF 豁免名单里 —— 匿名用户调用会被 403「CSRF token 缺失」"
    )


def test_send_code_without_csrf_is_not_403():
    """端到端复现原故障: 不带 cookie/CSRF 头直接 POST send-code, **不应是 403**。

    允许 4xx 业务错(如"发送过于频繁"/"邮箱格式不对"/'注册未开放'), 但**不许是 CSRF 403** ——
    这条判据把"中间件拦住了"和"业务自己拒绝"区分开。
    """
    c = TestClient(app)
    r = c.post("/api/auth/send-code", json={"email": "csrftest@example.com", "purpose": "register"})
    assert r.status_code != 403 or "CSRF" not in r.text
    assert "CSRF token 缺失" not in r.text


def test_authenticated_endpoints_stay_protected():
    """反向: 登录后才用的端点**不许**被豁免(别为了修这个洞把 CSRF 拆了)。"""
    for path in ("/api/auth/change-password", "/api/auth/users", "/api/admin/rotate-secrets"):
        assert not any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES), f"{path} 不该豁免 CSRF"


# ── API Key(机器调用)也必须免 CSRF(2026-09-20 生产实踩)────────────────────────
# 症状: Skills API 的 **GET 列表正常**(/api/skills 200), 但**跑 skill 的 POST**
# `/api/skills/{name}/run` 带 X-API-Key 一律 403「CSRF token 缺失, 请重新登录」——
# 也就是说对外发布的 Skills 接口,**核心动作面整个不可用**, 而列表面看着是好的(所以久未被发现)。
# 原理: CSRF 的前提是"浏览器会自动带上 cookie 凭证"; 机器调用带 X-API-Key, 没有这个前提。
def test_api_key_post_is_not_csrf_blocked():
    c = TestClient(app)
    r = c.post(
        "/api/skills/get_stock_quote/run",
        json={"symbol": "002361"},
        headers={"X-API-Key": "sk_" + "x" * 43},
    )
    # 允许 401「无效的 API Key」(这把我们没签过), 但**不许**是 CSRF 403
    assert "CSRF" not in r.text, f"带 X-API-Key 的 POST 被 CSRF 拦了: {r.status_code} {r.text[:120]}"


def test_post_without_any_credential_still_blocked():
    """反向: 什么都不带(既不登录也不带 API Key)的 POST 必须继续被 CSRF 拦。

    否则这次"给 API Key 开口子"就等于把 CSRF 整个拆了。
    """
    c = TestClient(app)
    r = c.post("/api/skills/get_stock_quote/run", json={"symbol": "002361"})
    assert r.status_code == 403 and "CSRF" in r.text, f"裸 POST 竟然没被拦: {r.status_code} {r.text[:120]}"
