"""web 层权限依赖工厂 —— 把 core 的 `enforce_perm` 接成 FastAPI 依赖。

**为什么单独一个文件**: 架构守卫要求 `src/core/*` 不 import FastAPI(见
`core/permissions.py` 里 B4.1 棘轮的说明), 所以"需要 FastAPI 依赖的调用点写在 web 层"。
路由级挂 `dependencies=[require_perm(...)]` 就能整组端点收口, 不用逐个函数里写判权。

用法::

    app.include_router(
        resonance.router,
        prefix="/api/resonance",
        dependencies=protected + [require_perm(PERM_VIEW_FORECAST)],
    )

2026-09-18: 数智决策三指标(view_forecast)与集合竞价池(view_auction)按老板口径收到 pro 档,
member 是否还能试用由 `core/free_tier` 的**运行时免费档**决定(默认已经不给了)。
"""
from __future__ import annotations

import logging
from typing import Callable

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from src.core.permissions import enforce_perm
from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import User

logger = logging.getLogger(__name__)


def require_perm(perm: str) -> Callable[..., None]:
    """返回一个 FastAPI 依赖: 校验当前用户对该权限点有访问资格。

    判定完全复用 core 的 `enforce_perm`(含免费档试用计数与 403 pro 引导), 保证
    HTTP API / skill / 聊天工具三条入口**同一套口径**, 不会出现"某条路漏判"。
    """

    def _dep(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> None:
        try:
            enforce_perm(user, perm, db)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            # 判权本身出错时**保守拒绝**(不放行), 但把原因写日志便于排查
            logger.warning("enforce_perm(%s) 异常, 保守拒绝: %r", perm, exc)
            raise HTTPException(403, f"无权限: {perm}") from exc

    return _dep
