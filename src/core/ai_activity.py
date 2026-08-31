# -*- coding: utf-8 -*-
"""AI 机构活跃度(阶段1 五件套 ④)。

算法内核严格复用 `decision_pioneer.compute_institution_activity`,
本模块只做规格语义封装, 不改公式:

    活跃度 = max(7 因子) × 1.2
    7 因子(全百分比): 上影 / 下影 / 实体+上影 / 实体+下影 / 上影+下影 / 涨幅 / 高开
    ⚠️ 是"7 因子取最大值 × 1.2", 不是"6 因子含量比"(那是错误设计)

## 阈值(官方, OCR 截图确认)
    生命线 +1.56 / 强势线 +3.00 / 大牛线 +6.00
    站上强势线 = 少数强势机构参与; 站上大牛线 = 短期多支一线强势机构参与

## 数据
日K(开/高/低/收/量), 至少 2 根。缺失显式 None / "无数据", 不编造。
"""

from __future__ import annotations

from typing import Optional, Sequence

from src.core.decision_pioneer import compute_institution_activity

LIFE_LINE = 1.56
STRONG_LINE = 3.00
BULL_LINE = 6.00

# 档位判定输入(共振状态机用)
LEVEL_WEAK = "弱"
LEVEL_LIFE = "生命"
LEVEL_STRONG = "强势"
LEVEL_BULL = "大牛"


def eval_activity(bars: Sequence[dict]) -> dict:
    """计算 AI 机构活跃度(官方语义)。

    Returns:
        {
          "activity": float | None,     # 当日活跃度
          "level": "弱"/"生命"/"强势"/"大牛" | None,
          "above_strong": bool | None,  # 站上强势线(>= 3.00), 共振状态机核心输入
          "above_bull": bool | None,    # 站上大牛线(>= 6.00)
          "streak_days": int,           # 活跃度 > 生命线连续日数
          "ma5": float | None,
        }
        数据不足 → activity/level/above_* 为 None + note "无数据"。
    """
    act = compute_institution_activity(list(bars or []))
    if not act:
        return {
            "activity": None, "level": None,
            "above_strong": None, "above_bull": None,
            "streak_days": 0, "ma5": None,
            "note": "无数据(日K < 2 根)",
        }
    a = act.get("activity")
    return {
        "activity": a,
        "level": act.get("level"),
        "above_strong": (a >= STRONG_LINE) if isinstance(a, (int, float)) else None,
        "above_bull": (a >= BULL_LINE) if isinstance(a, (int, float)) else None,
        "streak_days": act.get("streak_days", 0),
        "ma5": act.get("ma5"),
    }


def activity_of_value(value: Optional[float]) -> dict:
    """已知活跃度值 → 档位/线位判定(不写数据, 纯函数, 供状态机复用)。"""
    if not isinstance(value, (int, float)):
        return {"activity": None, "level": None, "above_strong": None, "above_bull": None}
    if value >= BULL_LINE:
        level = LEVEL_BULL
    elif value >= STRONG_LINE:
        level = LEVEL_STRONG
    elif value >= LIFE_LINE:
        level = LEVEL_LIFE
    else:
        level = LEVEL_WEAK
    return {
        "activity": value,
        "level": level,
        "above_strong": value >= STRONG_LINE,
        "above_bull": value >= BULL_LINE,
    }
