"""C3(2026-09-09) 统一多用户数据访问 helpers。

背景: 用户隔离靠每个端点手写 filter, 写漏就是越权(price_alerts hits/today、
abnormal_moves 全库扫描都是实际案例)。本模块收敛三种标准姿势:

- 读列表:   scoped(q, user)          自己的 + 全局共享(user_id IS NULL)
- 读单条:   owned_or_404(obj, user)  不存在与他人资源统一 404(防枚举探测)
- 写单条:   writable(obj, user)      只能改自己的; 全局共享项仅 owner 可改

全仓端点必须走这三个 helper; scripts/check_scoped_queries.py(CI 门禁)静态扫描
db.query(带 user_id 的模型), 没有过滤/豁免即红。
确需跨用户读的站点(display-only 名称回填等)用 @allow_cross_user 显式豁免。
"""
from __future__ import annotations

from typing import Callable, TypeVar

from fastapi import HTTPException
from sqlalchemy.orm import Query

from src.web.models import User

T = TypeVar("T")


def scoped(q: Query[T], user: User) -> Query[T]:
    """读: 自己的 + 全局共享(user_id IS NULL)。对应 stocks.py 的标准写法。"""
    entity = q.column_descriptions[0]["entity"]
    return q.filter(
        (entity.user_id == user.id) | (entity.user_id.is_(None))  # type: ignore[attr-defined]
    )


def owned_or_404(obj: T | None, user: User, what: str = "资源") -> T:
    """按主键取单条后校验归属。不存在与他人资源统一 404, 避免枚举探测。"""
    if obj is None:
        raise HTTPException(404, f"{what}不存在")
    if obj.user_id is not None and obj.user_id != user.id:
        # 故意 404 而非 403: 不向非归属者泄露资源存在性
        raise HTTPException(404, f"{what}不存在")
    return obj


def writable(obj: T, user: User, what: str = "资源") -> T:
    """写: 只能改自己的; 全局共享项(user_id NULL)仅 owner 可改。"""
    if obj.user_id is not None and obj.user_id != user.id:
        raise HTTPException(403, f"无权修改他人{what}")
    if obj.user_id is None and user.role != "owner":
        raise HTTPException(403, f"共享{what}仅 owner 可修改")
    return obj


def allow_cross_user(func: Callable) -> Callable:
    """显式豁免标记: 该函数内的 db.query 允许跨用户(如 display-only 名称回填)。

    纯标记不改行为; scripts/check_scoped_queries.py 识别该装饰器跳过检查。
    用它必须附一句为什么跨用户是安全的注释, 便于后续审计。
    """
    return func
