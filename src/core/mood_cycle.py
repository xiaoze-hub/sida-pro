"""情绪周期 → 题材操作容许度映射(批次C C1, 2026-09-06 28号)。

不重复造轮子: 阶段判定直接复用 market_phase(7 阶段, EMA+2日确认+弱档否决,
每日 15:10 sync 已挂 cron)。本模块只做两件事:
1. PHASE_ALLOWANCE: 阶段 → 题材操作容许度(0-10), 埋伏评分的情绪维直接消费;
2. current_mood(): 读 market_phase_daily 最新行 → 当前情绪态(带 biz_cache 30s)。

口径(老板框架): 发酵/启动期是埋伏黄金窗, 高潮期只卖不买, 退潮期避雷
(妖股先锋组禁推), 冰点期找错杀+新题材萌芽, 主升可跟随。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 阶段 → 容许度(0-10) + 操作提示
PHASE_ALLOWANCE: dict[str, dict] = {
    "ice": {"allowance": 5, "hint": "冰点期: 找错杀+新题材萌芽, 埋伏仓位保守"},
    "ignite": {"allowance": 10, "hint": "启动/发酵期: 埋伏黄金窗, 题材容许度最高"},
    "rally": {"allowance": 8, "hint": "主升期: 可跟随, 注意二跳滞涨标的"},
    "climax": {"allowance": 0, "hint": "高潮期: 只卖不买, 埋伏候选全部禁推"},
    "ebb": {"allowance": 1, "hint": "退潮期: 避雷优先, 妖股先锋组禁推"},
    "repair": {"allowance": 4, "hint": "修复期: 观察新主线, 仓位半开"},
    "accumulating": {"allowance": 3, "hint": "积累中: 等待方向确认"},
}


def mood_from_phase(phase: str | None, label: str | None = None) -> dict:
    """阶段 key → 情绪态。未知/缺数据 → available=False(不编造)。"""
    if not phase:
        return {"available": False, "reason": "无阶段数据(market_phase 未 sync)"}
    info = PHASE_ALLOWANCE.get(phase)
    if info is None:
        return {"available": False, "reason": f"未知阶段 {phase}"}
    return {
        "available": True,
        "phase": phase,
        "label": label or phase,
        "allowance": info["allowance"],
        "hint": info["hint"],
        "veto": info["allowance"] == 0,  # 高潮期硬否决
        "demon_veto": phase in ("ebb", "climax"),  # 妖股先锋组禁推
    }


def current_mood() -> dict:
    """读 market_phase_daily 最新行 → 当前情绪态。永不抛异常。"""
    try:
        from sqlalchemy import desc

        from src.db.session import SessionLocal
        from src.db.models import MarketPhaseDaily

        db = SessionLocal()
        try:
            row = db.query(MarketPhaseDaily).order_by(desc(MarketPhaseDaily.date)).first()
        finally:
            db.close()
        if row is None:
            return mood_from_phase(None)
        label = None
        try:
            from src.core.market_phase import PHASE_LABELS

            label = PHASE_LABELS.get(row.phase or "", row.phase)
        except Exception:  # noqa: BLE001
            label = row.phase
        m = mood_from_phase(row.phase, label)
        m["date"] = row.date.isoformat() if hasattr(row.date, "isoformat") else str(row.date)
        return m
    except Exception as e:  # noqa: BLE001
        logger.warning("current_mood 读取失败: %s", e)
        return {"available": False, "reason": f"读取失败: {e}"}
