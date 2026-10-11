"""首封时间×封成比 强度因子 API(2026-10-11 胜率创新 P0-1, **影子运行**)。

只读展示 + 手动打标触发。**影子运行**: 本端点只读影子表, 不改任何现有信号/决策。

端点:
    GET  /api/limit-strength/shadow?date=YYYYMMDD → 当日强度标签(缺省最近一日)
    POST /api/limit-strength/shadow/run           → 手动触发一轮当日打标(运维/实测)

红线: 无影子数据 → available=false + note, **绝不编造**; 标签是证据非建议。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
def limit_strength_usage():
    """裸路径自说明。"""
    return {
        "endpoint": "/api/limit-strength",
        "usage": {
            "GET /shadow?date=YYYYMMDD": "当日首封×封成比强度标签(S/A/B/C); 缺省最近一日",
            "POST /shadow/run": "手动触发一轮当日打标(影子运行, 仅落影子表)",
        },
        "note": "影子运行: 每日盘后 cron 对当日涨停股打标, 只写影子表, 不改任何现有信号; "
                "标签为统计证据, 非交易建议",
    }


@router.get("/shadow")
def get_shadow(date: str | None = Query(None, description="YYYYMMDD; 缺省最近一日")):
    """当日强度标签 + 分桶计数。无数据 → available=false。"""
    try:
        from src.core.limit_strength_shadow import labels_for_date

        return labels_for_date(date)
    except Exception as e:  # noqa: BLE001
        logger.warning("limit-strength shadow 查询失败: %s", e)
        raise HTTPException(status_code=500, detail=f"shadow query failed: {e}")


@router.post("/shadow/run")
def run_shadow(date: str | None = Query(None, description="YYYYMMDD; 缺省今日")):
    """手动触发一轮当日打标(幂等 upsert)。仅落影子表, 不改现有信号。"""
    try:
        from src.core.limit_strength_shadow import collect_and_mark

        return collect_and_mark(trade_date=date.replace("-", "") if date else None)
    except Exception as e:  # noqa: BLE001
        logger.warning("limit-strength shadow 打标失败: %s", e)
        raise HTTPException(status_code=500, detail=f"shadow run failed: {e}")
