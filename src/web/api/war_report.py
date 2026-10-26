"""主力资金战报 API(规格 §4.4, 2026-10-10)。

- `GET  /api/war-report/daily?market=CN`      读最新战报快照(无快照 → available:false + note)
- `POST /api/war-report/refresh`              手动触发构建 + 落库(owner, 全市场 DDE ~16s)

口径: 主力净流入为同花顺 DDE 大单口径(资金面参考), 禁用于主力意图方向性判定;
任一子块缺源显式 available=False + note, 不编造。权限: 数智决策档(view_forecast)。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.db.models import User
from src.web.api.auth import get_current_user, require_owner
from src.web.database import get_db
from src.web.models import WarReportDaily

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/daily")
def get_daily_war_report(
    market: str = "CN",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """读最新主力资金战报快照。权限: view_forecast(数智决策档)。"""
    from src.core.permissions import PERM_VIEW_FORECAST, enforce_perm

    enforce_perm(user, PERM_VIEW_FORECAST, db)
    row = (
        db.query(WarReportDaily)
        .filter(WarReportDaily.stock_market == market)
        .order_by(WarReportDaily.snapshot_date.desc(), WarReportDaily.id.desc())
        .first()
    )
    if not row:
        return {
            "available": False,
            "note": "暂无主力资金战报快照(盘后 cron 落库, 或 POST /api/war-report/refresh 手动触发)",
        }
    payload = row.payload if isinstance(row.payload, dict) else {}
    return {
        "available": True,
        "snapshot_date": row.snapshot_date,
        "market": row.stock_market,
        "updated_at": row.created_at.isoformat() if row.created_at else None,
        **payload,
    }


# 安全审计: 全市场 DDE 扫描耗时~16s, 仅 owner 可手动触发(防 DoS)
@router.post("/refresh")
def refresh_daily_war_report(_owner: User = Depends(require_owner)):
    """手动触发主力资金战报构建 + 落库(同步, 全市场约 16s)。数据源不可用 → 503。"""
    from src.core.war_report import run_war_report_job

    out = run_war_report_job()
    if not out.get("ok"):
        raise HTTPException(status_code=503, detail=out.get("reason") or "战报构建失败")
    return {"available": True, **out.get("report", {})}
