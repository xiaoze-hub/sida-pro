"""决策合成口(2026-09-08 方向2): GET /api/decision/{symbol} → 动手/看看/别碰 + 一行理由。"""
import asyncio
import logging

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{symbol}")
async def get_decision(symbol: str, market: str = "CN"):
    """三信号合成决策。永不 500: 算不出就看看 + 理由。"""
    from src.core.decision import decide

    try:
        return await asyncio.to_thread(decide, symbol, market)
    except Exception as e:  # noqa: BLE001
        logger.warning("decision endpoint %s failed: %s", symbol, e)
        raise HTTPException(500, f"决策计算失败({symbol})")
