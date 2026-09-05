"""埋伏雷达 API(2026-09-05, v0.5.6)。

GET /api/catalyst/calendar?days=30  未来催化日历(解禁/宏观窗口)
GET /api/catalyst/ambush?topn=12    埋伏榜(规则漏斗+LLM, 耗时~1min, 手动触发)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from src.web.api.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/calendar")
async def catalyst_calendar(days: int = 30, user=Depends(get_current_user)):
    """未来催化日历。days 上限 90。"""
    from src.core.catalyst_calendar import get_calendar

    days = max(1, min(days, 90))
    items = get_calendar(days)
    return {"days": days, "count": len(items), "items": items}


@router.get("/ambush")
async def catalyst_ambush(topn: int = 12, user=Depends(get_current_user)):
    """埋伏榜(手动触发, 跑规则+LLM, 约 1 分钟)。"""
    import asyncio

    from src.core.catalyst_calendar import get_calendar
    from src.core.catalyst_screener import build_ambush_list

    topn = max(1, min(topn, 20))
    cal = get_calendar(30)
    items = await asyncio.to_thread(build_ambush_list, cal, None, topn)
    return {"count": len(items), "items": items}
