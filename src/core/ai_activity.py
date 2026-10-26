# -*- coding: utf-8 -*-
"""AI 机构活跃度(阶段1 五件套 ④)。

算法内核严格复用 `decision_pioneer.compute_institution_activity`,
本模块只做规格语义封装, 不改公式:

    活跃度 = max(7 因子) × 1.2
    7 因子(全百分比): 上影 / 下影 / 实体+上影 / 实体+下影 / 上影+下影 / 涨幅 / 高开
    ⚠️ 是"7 因子取最大值 × 1.2", 不是"6 因子含量比"(那是错误设计)

## 因子口径核对(2026-10-10, 规格 §3)
公开功能解析资料把 AI 机构活跃度描述为 "**6 个**技术指标(影线/涨幅/高开等) MAX × 1.2",
我方实现是 **7 因子**(见 `ACTIVITY_FACTORS`)。逐因子比对结论: **保留 7 因子**, 依据:

- 我方内部《数智决策 8 问 8 答 解析》(GD, 见 docs/research/决策先锋复刻_能力差距矩阵_研究.md
  §2.3/§3.3)记录的官方因子集即 **7 个**(上影/下影/实上/实下/全幅/涨幅/高开), 与代码逐个对得上
  ("实上"=实体+上影, "实下"=实体+下影, "全幅"=上影+下影);
- 该 7 因子是逆向自桌面通达信公式 `1_JGHYD_机构活跃度.txt`(`docs/数据源与算法接口设计_TDX_THS
  互补.md:15` 亦记 "max(7因子)×1.2"), 且与官方截图实测对齐(误差目标 ≤0.1)。

差异点 = 相对公开"6 指标"多了 **上影+下影(全幅)** 这一项; 因有内部 8 问 8 答 + 逆向公式双重依据,
**保留 7 因子** 并在 `FACTOR_CALIBER_NOTE` 里钉死该结论。若日后拿到官方精确公式证实为 6, 再回改。

## 阈值(官方, OCR 截图确认)
    生命线 +1.56 / 强势线 +3.00 / 大牛线 +6.00 / **更佳线 +12.00(规格 §3 ">12 更佳")**
    站上强势线 = 少数强势机构参与; 站上大牛线 = 短期多支一线强势机构参与; 站上更佳线 = 顶格。

    以上为内置默认值; 实际生效值经 `src.core.thresholds` 统一配置层读取(env
    `SIDA_THRESHOLD_LIFE_LINE/STRONG_LINE/BULL_LINE/TOP_LINE` 可覆盖), 未设 env 时与此处一致。

## 数据
日K(开/高/低/收/量), 至少 2 根。缺失显式 None / "无数据", 不编造。
"""

from __future__ import annotations

from typing import Optional, Sequence

from src.core import thresholds as _thresholds
from src.core.decision_pioneer import compute_institution_activity

# 阈值来源: 统一配置层 src.core.thresholds(默认 1.56/3.00/6.00/12.00, 支持 env SIDA_THRESHOLD_* 覆盖)。
# 决策路径**一律**走 _thresholds.*() 实时读取, 不再硬编码常量(审计 P1-3 可配置化)。

# ── 因子口径(2026-10-10 规格 §3 核对结论: 保留 7 因子) ──────────────────────
# 与 decision_pioneer.compute_institution_activity 的 7 因子一一对应(供口径核对钉死, 勿改顺序)。
ACTIVITY_FACTORS: tuple[str, ...] = (
    "上影",
    "下影",
    "实体+上影",
    "实体+下影",
    "上影+下影",
    "涨幅",
    "高开",
)

# 口径差异说明(机器可查: 测试断言含 "6" 与 "8问8答")。公开资料=6 指标(影线/涨幅/高开等),
# 我方=7 因子, 依据=内部《数智决策 8 问 8 答》§2.3 + 逆向 TDX 公式 1_JGHYD, 差异项=上影+下影(全幅)。
FACTOR_CALIBER_NOTE = (
    "公开功能解析=AI 机构活跃度 6 个技术指标(影线/涨幅/高开等) MAX×1.2; "
    "我方=7 因子(见 ACTIVITY_FACTORS), 多的 1 项=上影+下影(全幅)。"
    "依据: 内部《数智决策8问8答》解析(§2.3 记录官方 7 因子: 上影/下影/实上/实下/全幅/涨幅/高开)"
    " + 逆向桌面通达信公式 1_JGHYD_机构活跃度.txt, 与官方截图实测对齐 → 保留 7 因子。"
)

# 档位判定输入(共振状态机用)
LEVEL_WEAK = "弱"
LEVEL_LIFE = "生命"
LEVEL_STRONG = "强势"
LEVEL_BULL = "大牛"
LEVEL_TOP = "更佳"  # 活跃度 >= 更佳线 12.00(规格 §3 ">12 更佳")



def eval_activity(bars: Sequence[dict]) -> dict:
    """计算 AI 机构活跃度(官方语义)。

    Returns:
        {
          "activity": float | None,     # 当日活跃度
          "level": "弱"/"生命"/"强势"/"大牛"/"更佳" | None,
          "above_strong": bool | None,  # 站上强势线(>= 3.00), 共振状态机核心输入
          "above_bull": bool | None,    # 站上大牛线(>= 6.00)
          "above_top": bool | None,     # 站上更佳线(>= 12.00, 规格 §3 ">12 更佳")
          "streak_days": int,           # 活跃度 > 生命线连续日数
          "ma5": float | None,
        }
        数据不足 → activity/level/above_* 为 None + note "无数据"。
    """
    act = compute_institution_activity(list(bars or []))
    if not act:
        return {
            "activity": None, "level": None,
            "above_strong": None, "above_bull": None, "above_top": None,
            "streak_days": 0, "ma5": None,
            "note": "无数据(日K < 2 根)",
        }
    a = act.get("activity")
    is_num = isinstance(a, (int, float))
    return {
        "activity": a,
        "level": _level_of(a) if is_num else None,   # 档位实时读配置层(含 >12 更佳档)
        "above_strong": (a >= _thresholds.strong_line()) if is_num else None,
        "above_bull": (a >= _thresholds.bull_line()) if is_num else None,
        "above_top": (a >= _thresholds.top_line()) if is_num else None,
        "streak_days": act.get("streak_days", 0),
        "ma5": act.get("ma5"),
    }


def _level_of(value: float) -> str:
    """活跃度值 → 档位(实时读配置层, 四线五档; >= 更佳线 12 → 更佳)。"""
    if value >= _thresholds.top_line():
        return LEVEL_TOP
    if value >= _thresholds.bull_line():
        return LEVEL_BULL
    if value >= _thresholds.strong_line():
        return LEVEL_STRONG
    if value >= _thresholds.life_line():
        return LEVEL_LIFE
    return LEVEL_WEAK


def activity_of_value(value: Optional[float]) -> dict:
    """已知活跃度值 → 档位/线位判定(不写数据, 纯函数, 供状态机复用)。"""
    if not isinstance(value, (int, float)):
        return {"activity": None, "level": None, "above_strong": None,
                "above_bull": None, "above_top": None}
    strong = _thresholds.strong_line()
    bull = _thresholds.bull_line()
    top = _thresholds.top_line()
    return {
        "activity": value,
        "level": _level_of(value),
        "above_strong": value >= strong,
        "above_bull": value >= bull,
        "above_top": value >= top,
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
