# -*- coding: utf-8 -*-
"""三指标共振状态机(阶段1 五件套 ⑤)。

三指标: 趋势(GS) + 活跃度(AI 机构活跃度) + 资金(主力明盘+暗盘净额)。
口诀: 要顺不要逆(趋势 G)、要活不要死(活跃度强)、要红不要绿(资金流入)。

## 完整 7 行状态表(决策先锋买卖体系, 官方 OCR 截图补全)

| 趋势 | 活跃度 | 资金 | 判断 | 四态 |
|------|--------|------|------|------|
| G信号 | 强势线上 | 流入 | 三指标首次共振 → 机会挖掘 | 向好 |
| G区间 | 强势线上(较前日翻倍) | 流入(较前日翻倍) | 再次共振 → 持续关注 | 拐点 |
| G区间 | 强势线上 | 流入 | 3指标平稳 → 关注动态 | 向好 |
| G区间 | 强势线上 | 流出 | 1个走坏 → 警惕主力出货 | 分歧 |
| G区间 | 跌破强势线 | 流入 | 1个走坏 → 主力还没出 | 分歧 |
| G区间 | 跌破强势线 | 流出 | 2个走坏 → 警惕 | 分歧 |
| S信号 | 跌破强势线 | 流出 | 全走坏 → 注意风险 | 走坏 |

## 显示口径
主力资金净额 = 明盘净额 + 暗盘净额; >0 流入(红) / <0 流出(绿);
支持 1/3/5 日周期; 0 轴上穿/下穿定多空。

## 回测基准(官方 2024.10-2025.10, 复刻版对照)
向好 75.42%/3.45, 拐点 73.48%/3.57, 分歧 70.55%/3.37, 走坏 67.40%/3.29

## 硬规则
输入缺失(None)一律显式标 "无数据", 不猜测; "较前日翻倍"缺前值按不满足处理。
"""

from __future__ import annotations

from typing import Optional

STRONG_LINE = 3.00  # 强势线(活跃度阈值)

# 四态回测基准(官方 2024.10-2025.10)
BACKTEST = {
    "向好": {"win_rate": 0.7542, "profit_ratio": 3.45},
    "拐点": {"win_rate": 0.7348, "profit_ratio": 3.57},
    "分歧": {"win_rate": 0.7055, "profit_ratio": 3.37},
    "走坏": {"win_rate": 0.6740, "profit_ratio": 3.29},
}


def _is_double(cur: Optional[float], prev: Optional[float]) -> bool:
    """较前一日翻倍。缺前值/前值为非正 → False(不猜)。"""
    if not isinstance(cur, (int, float)) or not isinstance(prev, (int, float)):
        return False
    if prev <= 0:
        return False
    return cur >= 2 * prev


def evaluate_state(
    trend: str,
    activity: Optional[float],
    activity_prev: Optional[float],
    fund_net: Optional[float],
    fund_net_prev: Optional[float],
) -> dict:
    """7 行状态表判定。

    Args:
        trend: "G信号" / "G区间" / "S信号" / "S区间" / "无数据"(见 gs_strategy.trend_label)
        activity: 当日 AI 机构活跃度(数值); 缺失 None
        activity_prev: 前一日活跃度(判"较前日翻倍"); 缺失 None
        fund_net: 当日主力资金净额 = 明盘 + 暗盘(元); 缺失 None
        fund_net_prev: 前一日主力资金净额(元); 缺失 None

    Returns:
        {
          "row": 0..7,            # 状态表行号(0 = 非表内)
          "state": str,           # 状态名
          "phase": "向好"/"拐点"/"分歧"/"走坏"/"无",   # 四态(回测对照)
          "action": str,          # 操作思路
          "bad_count": int,       # 走坏指标数(0-3)
          "backtest": {...}|None, # 四态回测基准
          "note": str | None,     # 缺失说明
        }
    """
    # 输入缺失 → 显式无数据
    missing = []
    if not trend or trend == "无数据":
        missing.append("趋势")
    if activity is None:
        missing.append("活跃度")
    if fund_net is None:
        missing.append("资金")
    if missing:
        return {
            "row": 0, "state": "无数据", "phase": "无",
            "action": "数据缺失, 不判定", "bad_count": 0,
            "backtest": None,
            "note": "缺失: " + "/".join(missing),
        }

    act_ok = activity >= STRONG_LINE
    act_double = _is_double(activity, activity_prev)
    fund_in = fund_net > 0
    fund_double = _is_double(fund_net, fund_net_prev)

    # 走坏指标数(趋势非G / 活跃度跌破强势线 / 资金流出)
    bad = (0 if trend in ("G信号", "G区间") else 1) + (0 if act_ok else 1) + (0 if fund_in else 1)

    # 行7: S信号 + 跌破强势线 + 流出 → 全走坏
    if trend == "S信号" and not act_ok and not fund_in:
        return _row(7, "全走坏", "走坏", "注意潜在风险", bad)

    if trend == "G信号":
        # 行1: G信号 + 强势线上 + 流入 → 首次共振(其余组合未在官方表内, 归"无共振")
        if act_ok and fund_in:
            return _row(1, "首次共振", "向好", "机会挖掘", bad)
        return _row(0, "无共振", "无", "观望", bad)

    if trend == "G区间":
        if act_ok and fund_in:
            # 行2: 双翻倍 → 再次共振; 行3: 平稳
            if act_double and fund_double:
                return _row(2, "再次共振", "拐点", "持续关注机会", bad)
            return _row(3, "平稳", "向好", "关注指标动态", bad)
        if act_ok and not fund_in:
            # 行4: 1个走坏(资金流出) → 警惕主力出货
            return _row(4, "1个走坏", "分歧", "警惕-主力出货多注意风险", bad)
        if not act_ok and fund_in:
            # 行5: 1个走坏(活跃度跌破) → 主力还没出
            return _row(5, "1个走坏", "分歧", "警惕-主力还没出持续关注", bad)
        # 行6: 2个走坏(活跃度跌破 + 资金流出)
        return _row(6, "2个走坏", "分歧", "警惕-具体幅度根据行情", bad)

    # S区间 / 其他 → 非表内
    return _row(0, "无共振", "无", "观望", bad)


def _row(row: int, state: str, phase: str, action: str, bad: int) -> dict:
    return {
        "row": row,
        "state": state,
        "phase": phase,
        "action": action,
        "bad_count": bad,
        "backtest": BACKTEST.get(phase),
        "note": None,
    }


def fund_flow_label(ming_net: Optional[float], dark_net: Optional[float]) -> dict:
    """主力资金显示口径: 净额 = 明盘 + 暗盘; >0 流入(红) / <0 流出(绿)。

    任一侧缺失 → 净额 None(显式无数据, 不把单侧当全量)。
    """
    if ming_net is None or dark_net is None:
        return {"net": None, "direction": "无数据", "color": None}
    net = ming_net + dark_net
    return {
        "net": net,
        "direction": "流入" if net > 0 else ("流出" if net < 0 else "平衡"),
        "color": "red" if net > 0 else ("green" if net < 0 else "gray"),
    }
