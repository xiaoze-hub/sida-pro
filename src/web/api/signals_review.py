"""信号复盘 API(批次D, 2026-09-06 28号)。

GET  /hit-rate       命中率统计(按信号类型, T+1/T+5 胜率+均值; resonance 对照官方基准)
POST /review         手动触发对账(运维; 默认每晚 18:30 cron 自动)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/hit-rate")
def signals_hit_rate(signal_type: str | None = None, days: int = 30):
    try:
        from src.core.signal_review import hit_rate

        return hit_rate(signal_type=signal_type, days=max(1, min(days, 365)))
    except Exception as e:  # noqa: BLE001
        logger.warning("hit-rate 查询失败: %s", e)
        raise HTTPException(status_code=500, detail=f"hit-rate failed: {e}")


@router.post("/review")
def signals_review(days_back: int = 3):
    try:
        from src.core.signal_review import nightly_review

        return nightly_review(days_back=max(1, min(days_back, 30)))
    except Exception as e:  # noqa: BLE001
        logger.warning("signal review 触发失败: %s", e)
        raise HTTPException(status_code=500, detail=f"review failed: {e}")
