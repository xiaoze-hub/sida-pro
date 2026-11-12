"""上下文与后验评估 API。"""

from __future__ import annotations

import logging
from datetime import timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from src.config import Settings
from src.core.context_store import (
    cleanup_context_data,
    get_latest_news_topic_snapshot,
    get_recent_stock_context_snapshots,
    list_agent_prediction_outcomes,
    list_recent_agent_context_runs,
)
from src.core.prediction_outcome import evaluate_pending_prediction_outcomes
from src.web.api.auth import get_current_user, require_owner
from src.web.models import User

router = APIRouter(prefix="/context", tags=["context"])
logger = logging.getLogger(__name__)


def _format_datetime(dt) -> str:
    if not dt:
        return ""
    tz_name = Settings().app_timezone or "UTC"
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        tzinfo = timezone.utc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tzinfo)
    return dt.astimezone(tzinfo).isoformat()


class StockContextSnapshotResponse(BaseModel):
    id: int
    symbol: str
    market: str
    snapshot_date: str
    context_type: str
    quality: dict
    payload: dict
    created_at: str


class AgentContextRunResponse(BaseModel):
    id: int
    agent_name: str
    stock_symbol: str
    analysis_date: str
    quality: dict
    context_payload: dict
    created_at: str


class AgentPredictionOutcomeResponse(BaseModel):
    id: int
    agent_name: str
    stock_symbol: str
    stock_market: str
    prediction_date: str
    horizon_days: int
    action: str
    action_label: str
    confidence: float | None = None
    trigger_price: float | None = None
    outcome_price: float | None = None
    outcome_return_pct: float | None = None
    outcome_status: str
    meta: dict
    evaluated_at: str
    created_at: str


@router.get("/snapshots/{symbol}", response_model=list[StockContextSnapshotResponse])
def list_stock_context_snapshots(
    symbol: str,
    market: str = "CN",
    context_type: str | None = None,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=30, ge=1, le=200),
):
    rows = get_recent_stock_context_snapshots(
        symbol=symbol,
        market=market,
        context_type=context_type,
        days=days,
        limit=limit,
    )
    return [
        StockContextSnapshotResponse(
            id=r.id,
            symbol=r.symbol,
            market=r.market,
            snapshot_date=r.snapshot_date,
            context_type=r.context_type,
            quality=r.quality or {},
            payload=r.payload or {},
            created_at=_format_datetime(r.created_at),
        )
        for r in rows
    ]


@router.get("/topics/latest")
def get_latest_topic(window_days: int = Query(default=7, ge=1, le=90)):
    row = get_latest_news_topic_snapshot(window_days=window_days)
    if not row:
        return {"exists": False}
    return {
        "exists": True,
        "id": row.id,
        "snapshot_date": row.snapshot_date,
        "window_days": row.window_days,
        "summary": row.summary or "",
        "topics": row.topics or [],
        "sentiment": row.sentiment or "neutral",
        "symbols": row.symbols or [],
        "coverage": row.coverage or {},
        "created_at": _format_datetime(row.created_at),
    }


@router.get("/runs", response_model=list[AgentContextRunResponse])
def list_context_runs(
    agent_name: str,
    stock_symbol: str | None = None,
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=300),
):
    rows = list_recent_agent_context_runs(
        agent_name=agent_name,
        stock_symbol=stock_symbol,
        days=days,
        limit=limit,
    )
    return [
        AgentContextRunResponse(
            id=r.id,
            agent_name=r.agent_name,
            stock_symbol=r.stock_symbol,
            analysis_date=r.analysis_date,
            quality=r.quality or {},
            context_payload=r.context_payload or {},
            created_at=_format_datetime(r.created_at),
        )
        for r in rows
    ]


@router.get("/predictions", response_model=list[AgentPredictionOutcomeResponse])
def list_prediction_outcomes(
    user: User = Depends(get_current_user),
    agent_name: str | None = None,
    stock_symbol: str | None = None,
    status: str | None = None,
    days: int = Query(default=90, ge=1, le=720),
    limit: int = Query(default=200, ge=1, le=1000),
):
    """当前账号的预测后验列表。

    P0(2026-11-12): 本表加了 user_id 后, 这里必须按登录用户过滤 —— 原来不带用户维度,
    任何人登录都能看到所有账号(以及无法归属的历史)的预测, 既是越权也是"成绩"混算的入口。
    历史无归属行不返回(归属不可证明), 也不拿它们凑数。
    """
    rows = list_agent_prediction_outcomes(
        agent_name=agent_name,
        stock_symbol=stock_symbol,
        status=status,
        days=days,
        limit=limit,
        user_id=user.id,
    )
    return [
        AgentPredictionOutcomeResponse(
            id=r.id,
            agent_name=r.agent_name,
            stock_symbol=r.stock_symbol,
            stock_market=r.stock_market,
            prediction_date=r.prediction_date,
            horizon_days=r.horizon_days,
            action=r.action,
            action_label=r.action_label,
            confidence=r.confidence,
            trigger_price=r.trigger_price,
            outcome_price=r.outcome_price,
            outcome_return_pct=r.outcome_return_pct,
            outcome_status=r.outcome_status,
            meta=r.meta or {},
            evaluated_at=_format_datetime(r.evaluated_at),
            created_at=_format_datetime(r.created_at),
        )
        for r in rows
    ]


@router.post("/predictions/evaluate")
def evaluate_predictions(
    max_horizon_days: int = Query(default=10, ge=1, le=30),
    limit: int = Query(default=300, ge=1, le=2000),
    user_id: str | None = Query(
        default=None,
        description="只评估该用户的行; 省略=评估全部有归属的个人计分行(不会碰历史未知行/system 桶)",
    ),
    _admin: User = Depends(require_owner),
):
    """全局后验评估 —— 跨账户写接口, 必须服务端管理员身份（2026-11-12 二次修正）。

    原先没有鉴权: 任意登录用户 POST 一次就能改写**所有账号**的后验成绩。现在:
    - 只有 owner/admin(严格模式下还需 admin 受众 token)可调用;
    - 评估范围永远是"有归属的个人计分行", 不会碰历史未知归属行, 也不会把 system 桶
      并进任何账户;
    - 可用 `user_id` 把范围收敛到单个用户(管理员按用户排查时用)。
    """
    return evaluate_pending_prediction_outcomes(
        max_horizon_days=max_horizon_days,
        limit=limit,
        user_id=user_id,
    )


@router.post("/cleanup")
def cleanup_context(
    snapshot_days: int = Query(default=180, ge=30, le=2000),
    topic_days: int = Query(default=180, ge=30, le=2000),
    context_run_days: int = Query(default=180, ge=30, le=2000),
    outcome_days: int = Query(default=365, ge=60, le=4000),
    dry_run: bool = Query(
        default=True,
        description="默认只统计不删除; 只有显式 dry_run=false 且管理员确认后才真正执行",
    ),
    _admin: User = Depends(require_owner),
):
    """全局清理 —— 跨账户**删除**接口, 必须服务端管理员, 且默认不执行（2026-11-12）。

    二次修正: 原来是任意登录用户可调、直接按保留期批量删除(包括无法证明归属的历史行)。
    现在:
    - 只有 owner/admin 可调用;
    - `dry_run` 默认 True —— 只返回将被清理的条数, **不执行任何删除**;
    - 历史未知归属行(user_id/owner_scope 均为 NULL)永不删除, 只报 `legacy_unowned_preserved`。
    """
    if dry_run:
        return cleanup_context_data(
            snapshot_days=snapshot_days,
            topic_days=topic_days,
            context_run_days=context_run_days,
            outcome_days=outcome_days,
            dry_run=True,
        )
    logger.warning(
        "[context] 管理员触发真实清理: snapshot_days=%s topic_days=%s context_run_days=%s "
        "outcome_days=%s admin=%s",
        snapshot_days,
        topic_days,
        context_run_days,
        outcome_days,
        getattr(_admin, "id", None),
    )
    return cleanup_context_data(
        snapshot_days=snapshot_days,
        topic_days=topic_days,
        context_run_days=context_run_days,
        outcome_days=outcome_days,
        dry_run=False,
    )
