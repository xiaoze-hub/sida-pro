"""W2.2/E4 (2026-09-09) factory helpers — 落实 AGENTS.md
「Fixtures: use factory helpers for DB models」。

约定:
- 工厂默认只构造不落库(纯对象, 字段有合理默认值);
- 需要落库时由调用方拿 Session 逐个 add/commit —— 工厂不开 Session,
  避免与会话级 engine/事务语义打架;
- 唯一性字段(username)自动加随机后缀, 参数化/循环建多用户不用手编。
"""
from __future__ import annotations

import uuid
from typing import Any

from src.web.models import Notification, NotifyChannel, Stock, User


def make_user(
    username: str | None = None,
    *,
    id: str | None = None,
    role: str = "member",
    password_hash: str = "x",
    is_active: bool = True,
    token_version: int = 1,
    **kw: Any,
) -> User:
    return User(
        id=id or str(uuid.uuid4()),
        username=username or f"u_{uuid.uuid4().hex[:10]}",
        password_hash=password_hash,
        role=role,
        is_active=is_active,
        token_version=token_version,
        **kw,
    )


def make_stock(
    symbol: str,
    user_id: str | None = None,
    *,
    name: str = "测试股",
    market: str = "CN",
    **kw: Any,
) -> Stock:
    return Stock(symbol=symbol, user_id=user_id, name=name, market=market, **kw)


def make_notify_channel(
    name: str | None = None,
    user_id: str | None = None,
    *,
    type: str = "webhook",
    config: dict | None = None,
    enabled: bool = True,
    is_default: bool = False,
    **kw: Any,
) -> NotifyChannel:
    return NotifyChannel(
        name=name or f"ch_{uuid.uuid4().hex[:8]}",
        user_id=user_id,
        type=type,
        config=config if config is not None else {},
        enabled=enabled,
        is_default=is_default,
        **kw,
    )


def make_notification(
    title: str = "测试通知",
    user_id: str | None = None,
    *,
    body: str = "",
    level: str = "info",
    **kw: Any,
) -> Notification:
    return Notification(
        title=title,
        user_id=user_id,
        body=body or title,
        level=level,
        **kw,
    )
