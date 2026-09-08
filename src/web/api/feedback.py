import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, case
from sqlalchemy.orm import Session

from src.web.api._scope import owned_or_404
from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import StockSuggestion, SuggestionFeedback, User


logger = logging.getLogger(__name__)
router = APIRouter()


class FeedbackIn(BaseModel):
    suggestion_id: int
    useful: bool


@router.post("")
def submit_feedback(
    payload: FeedbackIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """C3(2026-09-09): 建议按归属校验(自己的 + 全局 NULL), 他人的统一 404 ——
    此前任意用户可对他人建议提交反馈并探测其存在性。"""
    sug = (
        db.query(StockSuggestion)
        .filter(StockSuggestion.id == payload.suggestion_id)
        .first()
    )
    owned_or_404(sug, user, "建议")

    fb = SuggestionFeedback(suggestion_id=payload.suggestion_id, useful=payload.useful)
    db.add(fb)
    db.commit()

    return {"ok": True}


@router.get("/stats")
def feedback_stats(
    days: int = Query(14, ge=1, le=365),
    db: Session = Depends(get_db),
):
    """反馈统计（基础版）"""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    # SQLite: date(created_at) yields YYYY-MM-DD
    day_col = func.date(SuggestionFeedback.created_at)

    rows = (
        db.query(
            day_col.label("day"),
            func.count(SuggestionFeedback.id).label("total"),
            func.sum(case((SuggestionFeedback.useful == True, 1), else_=0)).label(
                "useful"
            ),
        )
        .filter(SuggestionFeedback.created_at >= since)
        .group_by(day_col)
        .order_by(day_col.desc())
        .all()
    )

    by_day = []
    for r in rows:
        total = int(r.total or 0)
        useful = int(r.useful or 0)
        useless = total - useful
        by_day.append(
            {
                "day": str(r.day),
                "total": total,
                "useful": useful,
                "useless": useless,
                "useful_rate": (useful / total) if total else 0.0,
            }
        )

    rows2 = (
        db.query(
            StockSuggestion.agent_name.label("agent_name"),
            func.count(SuggestionFeedback.id).label("total"),
            func.sum(case((SuggestionFeedback.useful == True, 1), else_=0)).label(
                "useful"
            ),
        )
        .join(StockSuggestion, StockSuggestion.id == SuggestionFeedback.suggestion_id)
        .filter(SuggestionFeedback.created_at >= since)
        .group_by(StockSuggestion.agent_name)
        .order_by(func.count(SuggestionFeedback.id).desc())
        .all()
    )

    by_agent = []
    for r in rows2:
        total = int(r.total or 0)
        useful = int(r.useful or 0)
        by_agent.append(
            {
                "agent_name": r.agent_name or "",
                "total": total,
                "useful": useful,
                "useless": total - useful,
                "useful_rate": (useful / total) if total else 0.0,
            }
        )

    total_all = sum(d["total"] for d in by_day)
    useful_all = sum(d["useful"] for d in by_day)

    return {
        "range_days": days,
        "total": total_all,
        "useful": useful_all,
        "useless": total_all - useful_all,
        "useful_rate": (useful_all / total_all) if total_all else 0.0,
        "by_day": by_day,
        "by_agent": by_agent,
    }
