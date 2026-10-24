"""分析历史 API"""

import logging
from datetime import timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from pydantic import BaseModel

from src.web.database import get_db
from src.web.models import AgentConfig, AnalysisHistory, User
from src.web.api.auth import get_current_user
from src.config import Settings
from src.core.agent_catalog import (
    AGENT_KIND_CAPABILITY,
    AGENT_KIND_WORKFLOW,
    CAPABILITY_AGENT_NAMES,
    infer_agent_kind,
)


def _format_datetime(dt) -> str:
    """格式化时间为当前时区的 ISO 格式。"""
    if not dt:
        return ""

    tz_name = Settings().app_timezone or "UTC"
    try:
        tzinfo = ZoneInfo(tz_name)
    except Exception:
        tzinfo = timezone.utc

    # Deterministic rule:
    # - naive datetime: app 时区(北京)本地时间(2026-08-24 修复 +8 偏移)
    # - aware datetime: keep original timezone semantics
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tzinfo)

    return dt.astimezone(tzinfo).isoformat(timespec="seconds")


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/history", tags=["history"])


class HistoryResponse(BaseModel):
    id: int
    agent_name: str
    agent_kind: str = AGENT_KIND_WORKFLOW
    stock_symbol: str
    analysis_date: str
    title: str
    content: str
    suggestions: dict | None = (
        None  # 个股建议 {symbol: {action, action_label, reason, should_alert}}
    )
    news: list[dict] | None = None
    quality_overview: dict | None = None
    context_summary: dict | None = None
    context_payload: dict | None = None
    prompt_context: str | None = None
    prompt_stats: dict | None = None
    news_debug: dict | None = None
    created_at: str
    updated_at: str
    # 0.3(2026-09-08): 降级状态透出, 前端据此显示「未生成」而非伪装成正常分析
    status: str = "success"
    error: str | None = None

    class Config:
        from_attributes = True


@router.get("")
def list_history(
    agent_name: str | None = None,
    stock_symbol: str | None = None,
    kind: str = Query(default=AGENT_KIND_WORKFLOW),
    limit: int = Query(default=30, le=100),
    summary_only: bool = Query(default=False, description="v0.4.77: True=只返摘要(content/prompt/raw_data 全部省, 列表秒开)"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[HistoryResponse]:
    """获取分析历史列表

    S1(2026-08-23): 仅返回当前用户的历史; 历史遗留的 user_id=NULL 行也归当前用户可见
    (迁移 v122 已把存量行归属给 owner, 跨账号 NULL 不会出现)。接口始终 404 而非 403,
    防止账号探测。

    v0.4.77 优化: summary_only=true 时只返 id/agent/stock/date/title/created_at,
    content/prompt/raw_data/news/quality 全省, 单接口从 ~100KB 降到 ~1KB,
    Dashboard 首页打开不再被 history 阻塞。详情走 GET /history/{id}。
    """
    query = db.query(AnalysisHistory).filter(AnalysisHistory.user_id == user.id)

    if agent_name:
        query = query.filter(AnalysisHistory.agent_name == agent_name)
    if stock_symbol:
        query = query.filter(AnalysisHistory.stock_symbol == stock_symbol)
    kind_norm = (kind or "").strip().lower()
    if kind_norm == AGENT_KIND_CAPABILITY:
        query = query.filter(
            or_(
                AnalysisHistory.agent_kind_snapshot == AGENT_KIND_CAPABILITY,
                and_(
                    or_(
                        AnalysisHistory.agent_kind_snapshot.is_(None),
                        AnalysisHistory.agent_kind_snapshot == "",
                    ),
                    AnalysisHistory.agent_name.in_(CAPABILITY_AGENT_NAMES),
                ),
            )
        )
    elif kind_norm == AGENT_KIND_WORKFLOW:
        query = query.filter(
            or_(
                AnalysisHistory.agent_kind_snapshot == AGENT_KIND_WORKFLOW,
                and_(
                    or_(
                        AnalysisHistory.agent_kind_snapshot.is_(None),
                        AnalysisHistory.agent_kind_snapshot == "",
                    ),
                    ~AnalysisHistory.agent_name.in_(CAPABILITY_AGENT_NAMES),
                ),
            )
        )

    records = (
        query.order_by(
            AnalysisHistory.analysis_date.desc(),
            AnalysisHistory.updated_at.desc(),
            AnalysisHistory.id.desc(),
        )
        .limit(limit)
        .all()
    )

    return [
        HistoryResponse(
            id=r.id,
            agent_name=r.agent_name,
            agent_kind=(r.agent_kind_snapshot or infer_agent_kind(r.agent_name)),
            stock_symbol=r.stock_symbol,
            analysis_date=r.analysis_date,
            title=r.title or "",
            # v0.4.77: summary_only 列表省 content/raw_data 全部重字段
            content="" if summary_only else r.content,
            suggestions=None if summary_only else (r.raw_data.get("suggestions") if r.raw_data else None),
            news=None if summary_only else (r.raw_data.get("news") if r.raw_data else None),
            quality_overview=None if summary_only else (r.raw_data.get("quality_overview") if r.raw_data else None),
            context_summary=None if summary_only else (r.raw_data.get("context_summary") if r.raw_data else None),
            context_payload=None if summary_only else (r.raw_data.get("context_payload") if r.raw_data else None),
            prompt_context=None if summary_only else (r.raw_data.get("prompt_context") if r.raw_data else None),
            prompt_stats=None if summary_only else (r.raw_data.get("prompt_stats") if r.raw_data else None),
            news_debug=None if summary_only else (r.raw_data.get("news_debug") if r.raw_data else None),
            created_at=_format_datetime(r.created_at),
            updated_at=_format_datetime(r.updated_at),
            status=r.status or "success",
            error=r.error,
        )
        for r in records
    ]


@router.get("/{history_id}")
def get_history_detail(
    history_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> HistoryResponse:
    """获取单条分析详情(S1: 仅返回当前用户的记录, 否则 404 防账号探测)"""
    record = (
        db.query(AnalysisHistory)
        .filter(AnalysisHistory.id == history_id, AnalysisHistory.user_id == user.id)
        .first()
    )
    if not record:
        from fastapi import HTTPException

        raise HTTPException(404, "记录不存在")

    return HistoryResponse(
        id=record.id,
        agent_name=record.agent_name,
        agent_kind=(record.agent_kind_snapshot or infer_agent_kind(record.agent_name)),
        stock_symbol=record.stock_symbol,
        analysis_date=record.analysis_date,
        title=record.title or "",
        content=record.content,
        suggestions=record.raw_data.get("suggestions") if record.raw_data else None,
        news=record.raw_data.get("news") if record.raw_data else None,
        quality_overview=record.raw_data.get("quality_overview")
        if record.raw_data
        else None,
        context_summary=record.raw_data.get("context_summary")
        if record.raw_data
        else None,
        context_payload=record.raw_data.get("context_payload")
        if record.raw_data
        else None,
        prompt_context=record.raw_data.get("prompt_context")
        if record.raw_data
        else None,
        prompt_stats=record.raw_data.get("prompt_stats")
        if record.raw_data
        else None,
        news_debug=record.raw_data.get("news_debug")
        if record.raw_data
        else None,
        created_at=_format_datetime(record.created_at),
        updated_at=_format_datetime(record.updated_at),
        status=record.status or "success",
        error=record.error,
    )


@router.delete("/{history_id}")
def delete_history(
    history_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除单条历史记录(S1: 仅当前用户可删, 否则 404 防账号探测)"""
    record = (
        db.query(AnalysisHistory)
        .filter(AnalysisHistory.id == history_id, AnalysisHistory.user_id == user.id)
        .first()
    )
    if not record:
        from fastapi import HTTPException

        raise HTTPException(404, "记录不存在")

    db.delete(record)
    db.commit()
    return {"ok": True}

class RetryResponse(BaseModel):
    ok: bool
    queued: bool
    agent_name: str
    message: str


@router.post("/{history_id}/retry", response_model=RetryResponse)
def retry_history(
    history_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """重试一条**生成失败**的报告（幂等：同 agent+标的+日期 走 upsert 覆盖，不会堆重复行）。

    为什么不直接在前端放个按钮：失败态没有下一步就是死路。用户看到「限流」只能等着，
    没有任何补救动作。这里把它接到 `trigger_agent` —— 它会按该用户的自选/持仓重跑，
    失败的记录被同键覆盖成新结果（`save_analysis` 是 upsert）。

    守卫：
      · 不属于当前用户的行 → 403（系统行 `user_id is None` 只有 owner 能重试）
      · 已经是 success → 409（不需要重试，避免白烧一次 LLM）
      · agent 不存在 / 工作流 agent 被禁用 → 404 / 400（先让用户去开启，别静默失败）
    """
    record = db.query(AnalysisHistory).filter(AnalysisHistory.id == history_id).first()
    if not record:
        raise HTTPException(404, "报告记录不存在")

    is_owner = (getattr(user, "role", None) or "") == "owner"
    if record.user_id != user.id and not is_owner:
        raise HTTPException(403, "无权重试该报告")

    if (record.status or "success") == "success":
        raise HTTPException(409, "该报告已成功生成，无需重试")

    agent = db.query(AgentConfig).filter(AgentConfig.name == record.agent_name).first()
    if not agent:
        raise HTTPException(404, f"Agent {record.agent_name} 不存在")

    agent_kind = (agent.kind or "").strip() or infer_agent_kind(agent.name)
    if agent_kind == AGENT_KIND_WORKFLOW and not agent.enabled:
        raise HTTPException(400, f"Agent {record.agent_name} 未启用，请先在设置里开启")

    # 延迟导入: 避免 history ↔ agents/runtime 在模块加载期形成环
    from src.bootstrap.runtime import trigger_agent
    from src.web.api.agents import _spawn_async_run

    _spawn_async_run(
        trigger_agent,
        record.agent_name,
        user.id,
        name=f"retry_history:{history_id}",
        notify_label=(agent.display_name or record.agent_name),
    )
    logger.info(f"重试报告: id={history_id} agent={record.agent_name} user={user.id}")
    return RetryResponse(
        ok=True,
        queued=True,
        agent_name=record.agent_name,
        message="已提交后台重试，完成后会在通知里告诉你",
    )
