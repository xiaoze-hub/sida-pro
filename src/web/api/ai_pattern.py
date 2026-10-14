"""AI 看图形态 影子证据层 API(2026-10-14 胜率创新 · 主线一并行, **影子运行**)。

只读展示 + 手动触发一轮识别。**影子运行**: 本端点只读影子表, 不改任何现有信号/决策。

端点:
    GET  /api/ai-pattern/shadow?date=YYYYMMDD → 当日 AI 形态 + 规则形态对照(缺省最近一日)
    POST /api/ai-pattern/shadow/run           → 手动触发一轮当日识别(运维/实测)

红线: 无视觉模型/无影子数据 → available=false + note, **绝不编造形态**; 形态是证据非建议。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
def ai_pattern_usage():
    """裸路径自说明。"""
    return {
        "endpoint": "/api/ai-pattern",
        "usage": {
            "GET /shadow?date=YYYYMMDD": "当日 AI 看图形态 + 规则形态对照(agree/conflict/"
                                          "ai_only/rules_only); 缺省最近一日",
            "POST /shadow/run": "手动触发一轮当日识别(影子运行, 仅落影子表)",
        },
        "note": "影子运行: 每日盘后 cron 对自选∪持仓逐票做「渲染→视觉 LLM 认形态」, "
                "并与规则形态(kline_patterns)同日同票并排落库; 只写影子表, 不改任何现有"
                "信号。无视觉模型/调用失败 → 显式降级不出形态(不编造); 形态为统计证据, "
                "非交易建议",
    }


@router.get("/shadow")
def get_shadow(date: str | None = Query(None, description="YYYYMMDD; 缺省最近一日")):
    """当日 AI 形态 + 对照结果 + 分桶计数。无数据 → available=false。"""
    try:
        from src.core.ai_vision_pattern import patterns_for_date

        return patterns_for_date(date)
    except Exception as e:  # noqa: BLE001
        logger.warning("ai-pattern shadow 查询失败: %s", e)
        raise HTTPException(status_code=500, detail=f"shadow query failed: {e}")


@router.post("/shadow/run")
def run_shadow(date: str | None = Query(None, description="YYYYMMDD; 缺省今日")):
    """手动触发一轮当日识别(幂等 upsert)。仅落影子表, 不改现有信号。"""
    try:
        from src.core.ai_vision_pattern import collect_and_mark

        return collect_and_mark(
            trade_date=date.replace("-", "") if date else None
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("ai-pattern shadow 识别失败: %s", e)
        raise HTTPException(status_code=500, detail=f"shadow run failed: {e}")
