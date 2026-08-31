"""LLM 调用统计 API(2026-08-15): 查询 llm_usage 日志的汇总 + 明细。

用途: 设置页「AI 调用统计」区块 — 真实 token 用量/费用估算/按场景筛选。
费用为估算: 按模型名匹配单价(含 flash → 0.004 元/千 token, 其余 0.02), 标注估算。
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from src.web.database import get_db
from src.web.models import LLMUsage

router = APIRouter(tags=["llm-usage"])

# 费用估算单价(元/千 token): 按模型名关键字匹配; 可在 settings 后续做成可配置
PRICE_FLASH = 0.004   # Flash 级模型(含 "flash" 关键字)
PRICE_LARGE = 0.02    # 其他(大模型/推理模型)

SCENE_LABELS = {
    "chat": "对话助手",
    "premarket": "盘前报告",
    "postmarket": "盘后报告",
    "referee": "AI 裁判",
    "selfcheck": "自检",
    "insights": "个股洞察",
    "other": "其他",
}


def _est_cost(model_name: str, prompt_tokens: int, completion_tokens: int) -> float:
    """估算单次调用费用(元)。"""
    rate = PRICE_FLASH if "flash" in (model_name or "").lower() else PRICE_LARGE
    return (prompt_tokens + completion_tokens) / 1000.0 * rate


def _since_days(range_: str) -> datetime:
    days = {"day": 1, "7d": 7, "30d": 30}.get(range_, 1)
    return datetime.now() - timedelta(days=days)


@router.get("/llm-usage")
def llm_usage_stats(
    range: str = Query("day", pattern="^(day|7d|30d)$"),
    scene: str = Query("", description="场景筛选: chat/premarket/postmarket/referee/other, 空=全部"),
    db: Session = Depends(get_db),
):
    """调用统计: 汇总(区间 + 本月)+ 最近 50 条明细。认证由路由级 dependencies 处理。"""
    since = _since_days(range)

    q = db.query(LLMUsage).filter(LLMUsage.created_at >= since)
    if scene:
        q = q.filter(LLMUsage.scene == scene)
    rows = q.order_by(LLMUsage.created_at.desc()).limit(50).all()

    # 区间汇总
    calls, tokens = len(rows), 0
    cost = 0.0
    for r in rows:
        tokens += (r.prompt_tokens or 0) + (r.completion_tokens or 0)
        cost += _est_cost(r.model_name, r.prompt_tokens or 0, r.completion_tokens or 0)

    # 本月汇总(全部场景)
    month_start = datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    mq = db.query(LLMUsage).filter(LLMUsage.created_at >= month_start)
    if scene:
        mq = mq.filter(LLMUsage.scene == scene)
    mrows = mq.all()
    month_tokens = sum((r.prompt_tokens or 0) + (r.completion_tokens or 0) for r in mrows)
    month_cost = sum(_est_cost(r.model_name, r.prompt_tokens or 0, r.completion_tokens or 0) for r in mrows)

    return {
        "range": range,
        "scene": scene,
        "summary": {
            "calls": len(rows),
            "tokens": tokens,
            "cost": round(cost, 2),
            "month_calls": len(mrows),
            "month_cost": round(month_cost, 2),
            "month_tokens": month_tokens,
        },
        "items": [
            {
                "time": r.created_at.strftime("%m-%d %H:%M:%S") if r.created_at else "",
                "scene": SCENE_LABELS.get(r.scene, r.scene or "other"),
                "scene_key": r.scene or "other",
                "model": r.model_name or "",
                "tokens": (r.prompt_tokens or 0) + (r.completion_tokens or 0),
                "latency": f"{(r.latency_ms or 0) / 1000:.1f}s",
            }
            for r in rows
        ],
        "scenes": [{"key": k, "label": v} for k, v in SCENE_LABELS.items()],
        "note": "费用为估算(Flash 0.004 元/千 token, 其他 0.02), 以服务商账单为准",
    }
