"""封单成色 API(批次A A3, 2026-09-06 28号)。

GET /{symbol}      当日采样指标(seal_quality.compute_from_series)
GET /{symbol}/raw  原始采样序列(最近 N 行, 排查用)
POST /sample-now   手动触发一轮采样(运维/实测用)

红线: 数据不足/非涨停/采样停滞 → available=false + reason, 绝不编造。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/{symbol}")
def seal_quality_metrics(symbol: str):
    """当日封单成色指标。available=false 时 reason 说明原因。"""
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol required")
    try:
        from src.core.seal_quality import compute_from_series
        from src.core.seal_sampler import get_recent_samples

        samples = get_recent_samples(symbol)
        metrics = compute_from_series(samples)
        return {
            "symbol": symbol,
            "n_samples": len(samples),
            "metrics": metrics,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("seal-quality %s 查询失败: %s", symbol, e)
        raise HTTPException(status_code=500, detail=f"seal-quality query failed: {e}")


@router.get("/{symbol}/raw")
def seal_quality_raw(symbol: str, limit: int = 120):
    symbol = symbol.strip()
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol required")
    try:
        from src.core.seal_sampler import get_recent_samples

        return {"symbol": symbol, "samples": get_recent_samples(symbol, limit=min(max(limit, 1), 600))}
    except Exception as e:  # noqa: BLE001
        logger.warning("seal-quality raw %s 查询失败: %s", symbol, e)
        raise HTTPException(status_code=500, detail=f"seal-quality raw query failed: {e}")


class SampleNowBody(BaseModel):
    symbols: list[str] | None = None


@router.post("/sample-now")
def sample_now(body: SampleNowBody | None = None):
    """手动触发一轮采样(默认涨停池全量, 最多 30 只)。"""
    try:
        from src.core.seal_sampler import sample_tick

        return sample_tick((body.symbols if body else None) or None)
    except Exception as e:  # noqa: BLE001
        logger.warning("seal-quality sample-now 失败: %s", e)
        raise HTTPException(status_code=500, detail=f"sample-now failed: {e}")
