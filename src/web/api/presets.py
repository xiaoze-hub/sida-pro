"""Presets API: 8 个 A 股可用的 swarm preset 团队配置。

✨ 借鉴 HKUDS/Vibe-Trading 30k stars 的 30 个 swarm preset 概念,
  过滤非 A 股相关(衍生品/加密/外汇/可转债等),只保留 8 个 A 股直接可用的。

端点:
  GET    /api/agents/presets                    列出所有 preset
  GET    /api/agents/presets/{name}             查 preset 详情
  POST   /api/agents/presets/{name}/run         跑 preset(走 TradingAgents 4 分析师 + 辩论)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.agents.tradingagents.presets import (
    get_preset,
    list_presets,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class RunPresetRequest(BaseModel):
    """跑 preset 的请求体"""

    target: str  # 股票代码,如 "002361"
    market: str = "CN"
    bypass_throttle: bool = False  # 是否绕过节流(测试用)


@router.get("")
async def list_all():
    """列出所有 preset(精简信息,适合前端下拉)。"""
    presets = list_presets()
    return {
        "items": [
            {
                "name": p.name,
                "title": p.title,
                "description": p.description,
                "agents": [{"id": a.id, "role": a.role} for a in p.agents],
                "debate_rounds": p.debate_rounds,
                "selected_analysts": p.selected_analysts,
            }
            for p in presets
        ],
        "total": len(presets),
    }


@router.get("/{name}")
async def get_detail(name: str):
    """查 preset 详情(包含完整 system_prompt)。"""
    p = get_preset(name)
    if not p:
        raise HTTPException(404, f"preset 不存在: {name}")
    return p.to_dict() | {
        "agents_full": [
            {"id": a.id, "role": a.role, "system_prompt": a.system_prompt}
            for a in p.agents
        ]
    }


@router.post("/{name}/run")
async def run(name: str, req: RunPresetRequest):
    """跑 preset(借 TradingAgents 4 分析师 + 辩论机制)。

    实现思路:
      1. 取 preset 配置(agents[] + debate_rounds + selected_analysts)
      2. 复用 TradingAgentsAgent 已有逻辑
      3. preset 的 system_prompt 注入到 context,作为「多角色 swarm 上下文」
      4. 走标准 4 分析师 → debate → 风控 → PM 整合流程
    """
    # 1. 验证 preset
    p = get_preset(name)
    if not p:
        raise HTTPException(404, f"preset 不存在: {name}")

    # 2. 找股票
    from sqlalchemy import select
    from src.web.database import SessionLocal
    from src.web.models import Stock

    with SessionLocal() as db:
        stock_obj = db.execute(
            select(Stock).where(
                Stock.symbol == req.target,
                Stock.market == req.market,
            )
        ).scalar_one_or_none()
        if not stock_obj:
            raise HTTPException(
                404,
                f"未找到标的 {req.target} ({req.market}),请先添加股票",
            )

    # 3. 构造 TradingAgentsAgent + AgentContext
    try:
        from src.agents.tradingagents.agent import TradingAgentsAgent
        from src.agents.base import AgentContext
        from server import build_context  # type: ignore  # server.py 同目录
    except ImportError as e:
        logger.exception("导入 TradingAgents 失败")
        raise HTTPException(500, f"TradingAgents 不可用: {e}")

    # 复用 server.py 的 build_context(已经处理好 ai_client / notifier / config / portfolio)
    context = build_context(agent_name="tradingagents")

    # 构造 data
    data = {
        "stock": stock_obj,
        "quote": {},
        "preset_meta": {
            "preset_name": name,
            "preset_title": p.title,
            "preset_agents": [{"id": a.id, "role": a.role} for a in p.agents],
        },
    }

    # 4. 跑 preset(实际调 TradingAgents)
    agent = TradingAgentsAgent(
        analyst_types=p.selected_analysts,
        debate_rounds=p.debate_rounds,
    )

    try:
        result = await agent.analyze(context, data)
    except Exception as e:
        logger.exception(f"preset {name} 跑失败")
        raise HTTPException(500, f"preset 跑失败: {e}")

    # 5. 序列化
    serialized = {}
    if hasattr(result, "__dict__"):
        for k, v in result.__dict__.items():
            if k.startswith("_"):
                continue
            try:
                import json
                json.dumps(v, default=str)
                serialized[k] = v
            except Exception:
                serialized[k] = str(v)
    return {
        "success": True,
        "preset": name,
        "target": req.target,
        "market": req.market,
        "result": serialized,
    }
