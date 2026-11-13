"""历史无归属经验参考 / system 桶独立对账 API(2026-11-13)。

三条数据路的**接口层隔离**:

  * 个人命中率仍只在 `/api/profile/*`(按登录用户过滤), 本路由不提供任何个人口径;
  * `GET /legacy` 只回历史无归属行的**全局聚合**, 明确标注"非个人成绩";
  * `GET /system` 只回 system 桶的全局聚合, 与个人结果互不合并;
  * `POST /system/reconcile` 是 system 桶的**只读**行情对账预览, 仅管理员,
    恒定 `writes=0`;
  * `POST /system/reconcile/run` 是**持久化**对账(仅管理员): 只写 system 桶的 pending
    成熟行, 条件更新 + 单批事务, 结果带数据日期/版本/采集时间; 不改权重、不下单。

本路由不接受 `user_id` / `symbol` 之类参数: 没有"查别人"的入口, 也没有明细出口。
管理员的授权主体(owner)与数据归属(owner_scope='system', user_id=NULL)硬隔离 ——
手动触发不会把结果写进触发者账号, 也不会改变任何个人成绩。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.core.learning_reference import (
    legacy_reference_summary,
    reconcile_system_outcomes,
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


@router.post("/system/reconcile/run")
def system_reconcile_run(
    _admin: User = Depends(require_owner),
    db: Session = Depends(get_db),
    max_horizon_days: int = Query(default=10, ge=1, le=30),
    limit: int = Query(default=50, ge=1, le=500, description="本批最多处理行数(不是待对账总数)"),
    max_symbols: int = Query(default=40, ge=1, le=200, description="本批最多外呼标的数"),
    fetch_timeout_seconds: float = Query(
        default=10.0, ge=0.5, le=60.0, description="单标的行情外呼硬超时(秒)"
    ),
):
    """**持久化** system 桶成熟结果对账(管理员手动触发)。

    与只读预览(`POST /system/reconcile`)的区别: 这里真的把成熟结果写回 system 行,
    页面/汇总因此能看到可积累的已评估样本。写入边界:

      * 只写 `user_id IS NULL AND owner_scope='system'` 的 pending 行; 个人/历史行不读不写;
      * 停牌/缺当日收盘价/行情故障/超时/未来/当日未收盘 => unknown, 保持 pending, 不写假结果;
      * 条件更新 + 单批事务: 并发只有一个 runner 写入成功, 失败整体回滚;
      * 结果行写 `meta.reconcile`(数据日期/规则版本/采集时间/去重规则), 可审计;
      * 不改权重、不下单、不触发交易。

    授权主体与数据归属硬隔离: 本接口要求 owner(生产还要求 admin 受众 token), 但
    `owner_scope` 恒为 `system`、`user_id` 恒为 NULL —— 管理员的账号绝不会被写成这些
    结果的归属人; 接口也不接受 user_id/symbol 参数。
    """
    result = reconcile_system_outcomes(
        db,
        max_horizon_days=max_horizon_days,
        limit=limit,
        max_symbols=max_symbols,
        fetch_timeout_seconds=fetch_timeout_seconds,
        trigger="manual",
    )
    # 防御性断言: 持久对账绝不能碰个人/历史行, 也不能触发交易/调权。
    if result.get("owner_scope") != "system":
        raise RuntimeError("system 对账结果的 owner_scope 不是 system")
    if result.get("personal_rows_touched") or result.get("legacy_rows_touched"):
        raise RuntimeError("system 对账触碰了个人/历史行, 违反隔离契约")
    if result.get("orders_placed") or result.get("weights_changed") or result.get("trades_triggered"):
        raise RuntimeError("system 对账触发了交易/调权, 违反只读业务边界")
    result["authorization"] = {
        "principal": f"owner:{_admin.username}",
        "role": "owner",
        "owner_scope": "system",
        "system_principal": False,
        "can_trigger_manual": True,
    }
    logger.info(
        "system 持久对账(手动, owner=%s): total_pending=%s batch=%s evaluated=%s "
        "unknown(no_quote=%s no_base=%s kline_error=%s) writes=%s committed=%s",
        _admin.username,
        result.get("total_pending"),
        result.get("batch_size"),
        result.get("evaluated"),
        result.get("skipped_no_quote"),
        result.get("skipped_no_base_price"),
        result.get("kline_error_rows"),
        result.get("writes"),
        result.get("db_committed"),
    )
    return result
