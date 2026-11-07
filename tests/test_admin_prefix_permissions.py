"""管理区前缀鉴权（2026-10-27 线上事故回归）。

事故：pro 用户点首页「盘中扫描」报 `无权限访问该管理功能, 请联系管理员`。

根因：`POST /api/agents/intraday/scan` 是**用户侧功能**（扫当前用户自己的持仓与监测股），
但路径落在 `/api/agents` 这个管理区前缀下，需要 `manage_agents` —— 而该权限只有 owner 有：

    pro   14 个权限点，不含 manage_agents
    member 9 个权限点，不含 manage_agents
    owner 23 个权限点，含 manage_agents

所以 pro / member 一定会 403，**只有 owner 能用**。开发环境只有 owner 账号，
端到端根本复现不出来，这就是它漏到线上的原因 —— 因此把判定抽成纯函数并在此钉死。
"""
from __future__ import annotations

import pytest

from src.core.permissions import ROLE_PERMISSIONS
from src.web.app import _USER_FACING_ADMIN_EXCEPTIONS, admin_prefix_denied

PRO = set(ROLE_PERMISSIONS["pro"])
MEMBER = set(ROLE_PERMISSIONS["member"])
OWNER = set(ROLE_PERMISSIONS["owner"])
GUEST = set(ROLE_PERMISSIONS["guest"])


def test_pro_does_not_have_manage_agents():
    """事故前提：pro 没有 manage_agents。若哪天给 pro 加了这个权限，这条会红，
    提醒你重新审视「管理区前缀」与「用户功能」的边界，而不是默默改变行为。"""
    assert "manage_agents" not in PRO
    assert "manage_agents" not in MEMBER
    assert "manage_agents" in OWNER


@pytest.mark.parametrize("role_perms", [PRO, MEMBER, OWNER], ids=["pro", "member", "owner"])
def test_intraday_scan_allowed_for_every_logged_in_role(role_perms):
    """盘中扫描是用户功能：三种登录角色都必须放行。"""
    assert admin_prefix_denied("POST", "/api/agents/intraday/scan", role_perms) is False


def test_intraday_scan_tolerates_query_and_trailing_slash():
    assert admin_prefix_denied("POST", "/api/agents/intraday/scan?analyze=true", PRO) is False
    assert admin_prefix_denied("POST", "/api/agents/intraday/scan/", PRO) is False


def test_agent_admin_writes_still_require_manage_agents():
    """放行精确路径，不能顺手把整段 /api/agents 放开。"""
    assert admin_prefix_denied("POST", "/api/agents/daily_report/trigger", PRO) is True
    assert admin_prefix_denied("PUT", "/api/agents/daily_report", PRO) is True
    assert admin_prefix_denied("DELETE", "/api/agents/daily_report", PRO) is True
    # owner 有权限
    assert admin_prefix_denied("PUT", "/api/agents/daily_report", OWNER) is False


def test_agent_reads_stay_open_for_get():
    """/api/agents 的 GET 一直放行（个股页要拉 Agent 列表/能力）。"""
    assert admin_prefix_denied("GET", "/api/agents", PRO) is False
    assert admin_prefix_denied("GET", "/api/agents/capabilities", MEMBER) is False


def test_other_admin_prefixes_unchanged():
    assert admin_prefix_denied("POST", "/api/settings", PRO) is True
    assert admin_prefix_denied("GET", "/api/settings", PRO) is False       # 只读浏览例外
    assert admin_prefix_denied("POST", "/api/users", PRO) is True
    assert admin_prefix_denied("POST", "/api/admin/rotate-secrets", PRO) is True
    assert admin_prefix_denied("POST", "/api/datasources", PRO) is True


def test_guest_still_blocked_from_admin_prefixes():
    assert admin_prefix_denied("GET", "/api/users", GUEST) is True


def test_exception_set_is_exact_paths_only():
    """白名单必须是 (method, 精确路径)，不允许出现前缀通配 —— 防止以后有人图省事加个 /api/agents/* 。"""
    for method, path in _USER_FACING_ADMIN_EXCEPTIONS:
        assert method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
        assert "*" not in path
        assert path.startswith("/api/")
        assert path == path.rstrip("/")


def test_middleware_end_to_end_pro_is_not_403_for_intraday_scan(monkeypatch):
    """端到端：带 pro 身份请求盘中扫描，中间件不能再返回那条 403。

    不清 DB 也能测：把 `_resolve_user_auth` 打桩成 ("pro", set())，签发一个真实 JWT，
    用 TestClient 打过去。终点落在端点内部（可能因缺少上下文 4xx/5xx），
    这里只钉**不再是被管理区拦下**这一件事。
    """
    from datetime import datetime, timedelta, timezone

    import jwt

    import src.web.app as app_module
    from src.core.auth_tokens import JWT_ALGORITHM, get_jwt_secret

    monkeypatch.setattr(app_module, "_resolve_user_auth", lambda username: ("pro", set()))

    from fastapi.testclient import TestClient

    token = jwt.encode(
        {
            "sub": "pro-user",
            "username": "pro-user",
            "role": "pro",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        get_jwt_secret(),
        algorithm=JWT_ALGORITHM,
    )
    with TestClient(app_module.app) as client:
        resp = client.post("/api/agents/intraday/scan", headers={"Authorization": f"Bearer {token}"})
    body = resp.text
    assert not (resp.status_code == 403 and "无权限访问该管理功能" in body), (
        f"盘中扫描仍被管理区拦截: {resp.status_code} {body[:200]}"
    )


# ── 2026-11-07: /api/v1 版本别名绕过管理区鉴权(独立复审 P0) ────────────────
def test_version_alias_path_is_normalized_for_authz():
    """`/api/v1/x` 与 `/api/x` 必须是同一个判权结论。

    `ApiVersionAliasMiddleware` 在内层, 外层 demo 隔离中间件看到的仍是带 /v1 的
    原始路径 —— 不做归一, 前缀表就匹配不到, member 直接写穿管理区(实测 200)。
    """
    from src.web.app import _authz_path

    assert _authz_path("/api/v1/settings/http_proxy") == "/api/settings/http_proxy"
    assert _authz_path("/api/v1") == "/api"
    assert _authz_path("/api/settings/http_proxy") == "/api/settings/http_proxy"
    assert _authz_path("/api/v10/settings") == "/api/v10/settings"  # 只认 /api/v1
    for path in ("/api/settings", "/api/datasources", "/api/providers", "/api/users", "/api/admin"):
        assert admin_prefix_denied("PUT", "/api/v1" + path[len("/api"):], MEMBER) is True
        assert admin_prefix_denied("PUT", path, MEMBER) is True


def test_version_alias_still_allows_member_reads():
    """归一不能顺手把只读浏览也拦掉: 掩码后的 GET 仍对登录用户开放。"""
    from src.web.app import _authz_path

    for path in ("/api/settings", "/api/providers", "/api/agents"):
        assert admin_prefix_denied("GET", "/api/v1" + path[len("/api"):], MEMBER) is False
        assert _authz_path("/api/v1" + path[len("/api"):]) == path


def test_version_alias_does_not_reopen_intraday_scan():
    """用户功能例外按精确路径登记, 归一后仍要命中。"""
    assert admin_prefix_denied("POST", "/api/v1/agents/intraday/scan", PRO) is False


def test_middleware_blocks_member_admin_write_via_version_alias(monkeypatch):
    """端到端: member 带 /api/v1 前缀写数据源配置, 中间件必须 403(不再是 200 写穿)。"""
    from datetime import datetime, timedelta, timezone

    import jwt

    import src.web.app as app_module
    from fastapi.testclient import TestClient

    from src.core.auth_tokens import JWT_ALGORITHM, get_jwt_secret

    monkeypatch.setattr(app_module, "_resolve_user_auth", lambda username: ("member", set()))
    token = jwt.encode(
        {
            "sub": "member-user",
            "username": "member-user",
            "role": "member",
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        get_jwt_secret(),
        algorithm=JWT_ALGORITHM,
    )
    with TestClient(app_module.app) as client:
        resp = client.put(
            "/api/v1/datasources/1",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "pwned"},
        )
    assert resp.status_code == 403, f"{resp.status_code} {resp.text[:200]}"
    assert "无权限访问该管理功能" in resp.text
