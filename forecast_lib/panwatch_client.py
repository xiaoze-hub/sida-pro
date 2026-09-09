"""PanWatch HTTP client shared by forecast bridge modules.

认证(2026-09-08 风险方案 0.0): forecast 容器只持只读服务凭据 ——
``PANWATCH_SERVICE_TOKEN``(与主服务 ``SIDA_SERVICE_TOKEN`` 同值, 经 .env 注入),
请求头 ``X-Service-Token``。不再从共享数据库读 JWT 签名密钥自签令牌:
那等价于伪造 owner 全权凭据(能调任何 owner 接口, 含改配置/删数据),
且切 PG 后读到的是过期 secret, 表现为"莫名其妙全 401"。

非 Compose 部署可显式提供 ``PANWATCH_TOKEN`` 或
``PANWATCH_USERNAME``/``PANWATCH_PASSWORD``(真实用户登录, 走正常鉴权)。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# 仅缓存显式凭据登录换来的 Bearer token(真实用户 JWT, 有过期时间)
_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}


def get_panwatch_url() -> str:
    """Return the PanWatch base URL configured for this process.

    D5(2026-09-09): 主服务地址环境变量统一为 ``SIDA_MAIN_API_URL``(旧名已废弃,
    forecast_lib 内禁止出现旧名字样, 见 CI 依赖方向断言 tests/test_w36_dependency_direction.py);
    旧探测兜底(网关猜测/硬编码网关 IP)已随 forecast_sentiment 一并删除。
    """
    return os.getenv("SIDA_MAIN_API_URL", "http://127.0.0.1:8000").rstrip("/")


def get_service_token() -> str:
    """只读服务令牌(与主服务 SIDA_SERVICE_TOKEN 同值, compose 经 .env 注入)。"""
    return (
        os.getenv("PANWATCH_SERVICE_TOKEN", "").strip()
        or os.getenv("SIDA_SERVICE_TOKEN", "").strip()
    )


def invalidate_token() -> None:
    """Drop the cached login token (e.g. after a 401)."""
    _TOKEN_CACHE.update(token="", expires_at=0.0)


def _login_with_explicit_credentials() -> str:
    """Compatibility fallback for hosts without a service token."""
    username = os.getenv("PANWATCH_USERNAME") or os.getenv("AUTH_USERNAME")
    password = os.getenv("PANWATCH_PASSWORD") or os.getenv("AUTH_PASSWORD")
    if not username or not password:
        return ""
    request = urllib.request.Request(
        f"{get_panwatch_url()}/api/auth/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.loads(response.read())
    return data.get("data", {}).get("token", "")


def get_token() -> str:
    """Return an explicit token or a credential-login token; 无凭据返回空串。

    0.0: 自签 JWT 通道已删 —— forecast 侧没有任何 JWT 签名密钥来源,
    服务身份一律走 X-Service-Token(见 auth_headers)。
    """
    explicit = os.getenv("PANWATCH_TOKEN", "").strip()
    if explicit:
        return explicit

    now = time.time()
    cached = str(_TOKEN_CACHE.get("token", ""))
    if cached and now < float(_TOKEN_CACHE.get("expires_at", 0)) - 30:
        return cached

    try:
        token = _login_with_explicit_credentials()
    except Exception as exc:
        logger.warning("PanWatch 显式凭据登录失败: %s", exc)
        return ""
    if token:
        _TOKEN_CACHE.update(token=token, expires_at=now + 240)
    return token


def auth_headers() -> dict[str, str]:
    """构造 8000 请求头: 服务令牌优先(X-Service-Token), 其次 Bearer。

    两者都没有时返回空 dict —— 调用方据此显式报错, 不再静默裸奔。
    """
    headers: dict[str, str] = {}
    svc = get_service_token()
    if svc:
        headers["X-Service-Token"] = svc
    token = get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def has_credentials() -> bool:
    return bool(get_service_token() or get_token())


def request_json(path: str, timeout: float = 30) -> Any:
    """GET a protected PanWatch endpoint and retry once after a 401.

    认证双轨: X-Service-Token(服务令牌, 首选) / Bearer(显式 token 或凭据登录)。
    无任何凭据时抛 RuntimeError —— 失败必须可见, 不允许静默回落。
    """
    url = f"{get_panwatch_url()}/{path.lstrip('/')}"
    if not has_credentials():
        raise RuntimeError(
            "PanWatch 无可用凭据: 请在 forecast 容器注入 PANWATCH_SERVICE_TOKEN"
            "(与主服务 SIDA_SERVICE_TOKEN 同值)或 PANWATCH_TOKEN/账号密码"
        )
    for attempt in range(2):
        headers = auth_headers()
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0 and not os.getenv("PANWATCH_TOKEN"):
                invalidate_token()
                continue
            raise
    return None
