"""历史无归属经验参考 / system 桶独立对账 API(2026-11-13)。

三条数据路的**接口层隔离**:

  * 个人命中率仍只在 `/api/profile/*`(按登录用户过滤), 本路由不提供任何个人口径;
  * `GET /legacy` 只回历史无归属行的**全局聚合**, 明确标注"非个人成绩";
  * `GET /system` 只回 system 桶的全局聚合, 与个人结果互不合并;
  * `POST /system/reconcile` 是 system 桶的**只读**行情对账预览, 仅管理员,
    恒定 `writes=0` —— 不写回、不改权重、不下单。

本路由不接受 `user_id` / `symbol` 之类参数: 没有"查别人"的入口, 也没有明细出口。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.core.learning_reference import (
    legacy_reference_summary,
    system_reconcile_preview,
    system_reference_summary,
)
from src.web.api.auth import get_current_user, require_owner
from src.web.database import get_db
from src.web.models import User

router = APIRouter(tags=["learning-reference"])
logger = logging.getLogger(__name__)


@router.get("/legacy")
def legacy_reference(
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    days: int | None = Query(
        default=None,
        ge=1,
        le=4000,
        description="可选回看窗口; 省略=全部历史(旧数据通常较早, 默认不限窗口)",
    ),
    min_n: int = Query(default=30, ge=1, le=1000, description="分 Agent 参评最低样本"),
):
    """历史无归属(加列前存量)的全局经验参考。

    **只做全局汇总**: 不返回个股、用户标识或行级明细; 归属不可证明, 不计入任何人的命中率;
    来源时点不可证 ⇒ 不训练、不自动调权、不宣称回测。
    """
    return legacy_reference_summary(db, days=days, min_n=min_n)


@router.get("/system")
def system_reference(
    _user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    days: int | None = Query(default=None, ge=1, le=4000),
    min_n: int = Query(default=20, ge=1, le=1000),
):
    """system 桶(无会话系统调度运行)的独立全局聚合 —— 绝不并入任何个人成绩。"""
    return system_reference_summary(db, days=days, min_n=min_n)


@router.post("/system/reconcile")
def system_reconcile(
    _admin: User = Depends(require_owner),
    db: Session = Depends(get_db),
    max_horizon_days: int = Query(default=10, ge=1, le=30),
    limit: int = Query(default=50, ge=1, le=500),
):
    """system 桶的**只读**行情对账预览(管理员)。

    只统计"到期未评估"的 system 行, 返回本来会评估多少; 一行不写、不改权重、不下单。
    故意不提供真正回填的入口 —— system 行无个人归属, 不适合变成任何人的成绩来源。
    """
    result = system_reconcile_preview(
        db, max_horizon_days=max_horizon_days, limit=limit
    )
    if result.get("writes"):
        # 防御性断言: 预览路径不可能产生写入; 真出现就是代码缺陷, 必须显式报错而不是静默。
        raise RuntimeError("system 对账预览出现写入, 违反只读契约")
    logger.info(
        "system 只读对账预览: pending=%s would_evaluate=%s writes=0",
        result.get("total_pending"),
        result.get("would_evaluate"),
    )
    return result
