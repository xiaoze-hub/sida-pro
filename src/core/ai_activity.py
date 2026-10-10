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

    以上为内置默认值; 实际生效值经 `src.core.thresholds` 统一配置层读取(env
    `SIDA_THRESHOLD_LIFE_LINE/STRONG_LINE/BULL_LINE` 可覆盖), 未设 env 时与此处一致。

## 数据
日K(开/高/低/收/量), 至少 2 根。缺失显式 None / "无数据", 不编造。
"""

from __future__ import annotations

from typing import Optional, Sequence

from src.core import thresholds as _thresholds
from src.core.decision_pioneer import compute_institution_activity

# 阈值来源: 统一配置层 src.core.thresholds(默认 1.56/3.00/6.00, 支持 env SIDA_THRESHOLD_* 覆盖)。
# 决策路径**一律**走 _thresholds.*() 实时读取, 不再硬编码常量(审计 P1-3 可配置化)。

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
        "above_strong": (a >= _thresholds.strong_line()) if isinstance(a, (int, float)) else None,
        "above_bull": (a >= _thresholds.bull_line()) if isinstance(a, (int, float)) else None,
        "streak_days": act.get("streak_days", 0),
        "ma5": act.get("ma5"),
    }


def activity_of_value(value: Optional[float]) -> dict:
    """已知活跃度值 → 档位/线位判定(不写数据, 纯函数, 供状态机复用)。"""
    if not isinstance(value, (int, float)):
        return {"activity": None, "level": None, "above_strong": None, "above_bull": None}
    life = _thresholds.life_line()
    strong = _thresholds.strong_line()
    bull = _thresholds.bull_line()
    if value >= bull:
        level = LEVEL_BULL
    elif value >= strong:
        level = LEVEL_STRONG
    elif value >= life:
        level = LEVEL_LIFE
    else:
        level = LEVEL_WEAK
    return {
        "activity": value,
        "level": level,
        "above_strong": value >= strong,
        "above_bull": value >= bull,
    }


def activity_series(bars: Sequence[dict]) -> list[dict]:
    """活跃度日级序列(副图柱载体用, 与 klines 日期对齐)。

    逐前缀调 decision_pioneer.compute_institution_activity, 公式零分叉;
    n≈120 时 O(n²)≈7200 步简单运算, 可忽略, 故不碰已有函数。
    每点 {date, activity, level}; 第 0 根/算不出 → activity/level 为 None(断裂不断造)。
    """
    from src.core.decision_pioneer import compute_institution_activity

    out: list[dict] = []
    for i, b in enumerate(bars or []):
        date = b.get("date") if isinstance(b, dict) else None
        if i == 0:
            out.append({"date": date, "activity": None, "level": None})
            continue
        try:
            r = compute_institution_activity(list(bars[: i + 1]))
        except Exception:  # noqa: BLE001
            r = None
        if not r:
            out.append({"date": date, "activity": None, "level": None})
        else:
            out.append({"date": date, "activity": r.get("activity"), "level": r.get("level")})
    return out
