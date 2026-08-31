"""PanWatch HTTP client shared by forecast bridge modules.

Docker Compose deployments authenticate with a short-lived JWT signed from the
shared PanWatch database. This avoids copying a user's login password into the
forecast container. Non-Compose deployments may provide ``PANWATCH_TOKEN`` or
``PANWATCH_USERNAME``/``PANWATCH_PASSWORD`` explicitly.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_TOKEN_CACHE: dict[str, Any] = {"token": "", "expires_at": 0.0}


def get_panwatch_url() -> str:
    """Return the PanWatch base URL configured for this process."""
    return os.getenv("PANWATCH_URL", "http://127.0.0.1:8000").rstrip("/")


def _read_auth_settings() -> dict[str, str]:
    """Read JWT material from the shared PanWatch DB without modifying it."""
    db_paths = (
        os.getenv("PANWATCH_DB", ""),
        "/app/panwatch-data/panwatch.db",
        "/app/data/panwatch.db",
    )
    for path in db_paths:
        if not path or not os.path.isfile(path):
            continue
        try:
            with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3) as conn:
                settings = dict(
                    conn.execute(
                        "SELECT key, value FROM app_settings WHERE key IN "
                        "('jwt_secret', 'auth_token_version')"
                    ).fetchall()
                )
                # 服务 token 的 sub 必须是真实存在的用户 id(owner),否则 401
                try:
                    row = conn.execute(
                        "SELECT id FROM users WHERE role='owner' ORDER BY created_at LIMIT 1"
                    ).fetchone()
                    if row:
                        settings["owner_user_id"] = row[0]
                except sqlite3.Error:
                    pass
                return settings
        except sqlite3.Error as exc:
            logger.warning("PanWatch 认证配置读取失败 [%s]: %s", path, exc)
    return {}


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _create_service_token(secret: str, token_version: int, sub: str = "user") -> tuple[str, float]:
    """Create a five-minute HS256 token accepted by the PanWatch API.

    sub 必须是 PanWatch users 表里真实存在的用户 id(owner),否则
    auth.get_current_user 查不到用户 → 401。
    """
    now = int(time.time())
    expires_at = now + 300
    header = _base64url(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _base64url(
        json.dumps(
            {
                "exp": expires_at,
                "iat": now,
                "sub": sub,
                "jti": secrets.token_hex(16),
                "ver": token_version,
            },
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header}.{payload}"
    signature = _base64url(hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest())
    return f"{signing_input}.{signature}", float(expires_at)


def invalidate_token() -> None:
    """Drop the cached token so a changed token version is picked up."""
    _TOKEN_CACHE.update(token="", expires_at=0.0)


def _login_with_explicit_credentials() -> str:
    """Compatibility fallback for hosts without a shared PanWatch database."""
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
    """Return an explicit, database-signed, or credential-login token."""
    explicit = os.getenv("PANWATCH_TOKEN", "").strip()
    if explicit:
        return explicit

    now = time.time()
    cached = str(_TOKEN_CACHE.get("token", ""))
    if cached and now < float(_TOKEN_CACHE.get("expires_at", 0)) - 30:
        return cached

    settings = _read_auth_settings()
    secret = os.getenv("PANWATCH_JWT_SECRET", "").strip() or settings.get("jwt_secret", "")
    if secret:
        raw_version = settings.get("auth_token_version", "1")
        token_version = int(raw_version) if str(raw_version).isdigit() else 1
        owner_id = settings.get("owner_user_id", "") or os.getenv("PANWATCH_OWNER_ID", "").strip()
        token, expires_at = _create_service_token(secret, token_version, sub=owner_id or "user")
        _TOKEN_CACHE.update(token=token, expires_at=expires_at)
        return token

    try:
        token = _login_with_explicit_credentials()
    except Exception as exc:
        logger.warning("PanWatch 显式凭据登录失败: %s", exc)
        return ""
    if token:
        _TOKEN_CACHE.update(token=token, expires_at=now + 240)
    return token


def request_json(path: str, timeout: float = 30) -> Any:
    """GET a protected PanWatch endpoint and retry once after a 401."""
    url = f"{get_panwatch_url()}/{path.lstrip('/')}"
    for attempt in range(2):
        token = get_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
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
