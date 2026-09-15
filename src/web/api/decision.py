"""决策合成口(2026-09-08 方向2): GET /api/decision/{symbol} → 动手/看看/别碰 + 一行理由。

权限(2026-09-15): view_forecast — member 3次/天试用, pro/owner 全开。
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from src.web.api.auth import get_current_user
from src.web.database import get_db
from src.web.models import User

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{symbol}")
async def get_decision(
    symbol: str,
    market: str = "CN",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """三信号合成决策。永不 500: 算不出就看看 + 理由。"""
    from src.core.permissions import PERM_VIEW_FORECAST, enforce_perm

    enforce_perm(user, PERM_VIEW_FORECAST, db)
    from src.core.decision import decide

    try:
        return await asyncio.to_thread(decide, symbol, market)
    except Exception as e:  # noqa: BLE001
        logger.warning("decision endpoint %s failed: %s", symbol, e)
        raise HTTPException(500, f"决策计算失败({symbol})")
