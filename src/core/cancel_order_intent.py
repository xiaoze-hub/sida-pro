# -*- coding: utf-8 -*-
"""高频撤单意图分析引擎 (Cancel Order Intent Engine).

方法论对齐:
1. 广发金工《从高频撤单信息中构建因子: 海量 Level 2 数据因子挖掘系列(八)》(2026):
   - 撤单行为刻画的是投资者曾经提交但最终主动取消的"流动性承诺";
   - 区分撤买率、撤卖率、撤单倾斜度与撤单活跃度;
2. 本土主力博弈口径:
   - 虚假托盘 (Fake Bidding): 买盘撤单率高且价格滞涨 -> 挂大单诱多掩护出货;
   - 虚假压盘 (Fake Selling): 卖盘撤单率高且价格抗跌 -> 压盘恐吓散户、逆势吸筹;
   - 纯函数设计, 零外部网络/数据库依赖, 便于全量回测与严格单测。
"""
from __future__ import annotations

from typing import Any, Dict


def calc_cancel_order_intent(
    more_info: Dict[str, Any] | None,
    pct_chg: float | None = None,
) -> Dict[str, Any]:
    """计算单股高频撤单意图与微观盘口指标.

    入参:
        more_info: 来自通达信 TQ `get_more_info` 的字典, 至少包含:
            - BCancel: 买盘撤单量 (手)
            - SCancel: 卖盘撤单量 (手)
            - TotalBVol: 委买总量/申报笔数 (手/笔)
            - TotalSVol: 委卖总量/申报笔数 (手/笔)
            - L2OrderNum: 逐笔总委托笔数 (可选)
            - L2TicNum: 逐笔总成交笔数 (可选)
        pct_chg: 当日最新涨跌幅(%), 用于结合价格走势判定诱多/诱空。

    返回结构:
        {
            "available": bool,
            "b_cancel": float,           # 买盘撤单量(手)
            "s_cancel": float,           # 卖盘撤单量(手)
            "b_cancel_ratio": float,     # 买盘撤单率 (0~1)
            "s_cancel_ratio": float,     # 卖盘撤单率 (0~1)
            "cancel_bias": float,        # 撤单偏向 (-1~+1, >0 买方撤多, <0 卖方撤多)
            "activity_score": float,     # 撤单活跃度 (0~1)
            "intent_tag": str,           # 意图标签 (虚假托盘/压盘吸筹/多空均衡/无数据)
            "intent_desc": str,          # 意图详解说明
            "warning_level": str,        # safe / caution / warning
        }
    """
    if not more_info or not isinstance(more_info, dict):
        return {
            "available": False,
            "b_cancel": 0.0,
            "s_cancel": 0.0,
            "b_cancel_ratio": None,
            "s_cancel_ratio": None,
            "cancel_bias": 0.0,
            "activity_score": 0.0,
            "intent_tag": "无数据",
            "intent_desc": "暂无撤单盘口数据",
            "warning_level": "safe",
        }

    def _f(key: str) -> float:
        val = more_info.get(key)
        if val is None:
            return 0.0
        try:
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    b_cancel = _f("BCancel")
    s_cancel = _f("SCancel")
    total_b = _f("TotalBVol")
    total_s = _f("TotalSVol")
    l2_order_num = _f("L2OrderNum")

    total_cancel = b_cancel + s_cancel
    if total_cancel <= 0.0:
        return {
            "available": False,
            "b_cancel": 0.0,
            "s_cancel": 0.0,
            "b_cancel_ratio": None,
            "s_cancel_ratio": None,
            "cancel_bias": 0.0,
            "activity_score": 0.0,
            "intent_tag": "无撤单",
            "intent_desc": "当日盘口无有效撤单记录",
            "warning_level": "safe",
        }

    # 1. 撤买率与撤卖率 (撤单量 / (委托量 + 撤单量) 或 直接占比)
    # TQ 字段定义: TotalBVol / TotalSVol 是累计委买/委卖量, BCancel / SCancel 是累计撤单量
    b_denom = total_b if total_b > b_cancel else (total_b + b_cancel)
    s_denom = total_s if total_s > s_cancel else (total_s + s_cancel)

    b_ratio = (b_cancel / b_denom) if b_denom > 0 else 0.0
    s_ratio = (s_cancel / s_denom) if s_denom > 0 else 0.0

    # 规范在 0~1 之间
    b_ratio = min(1.0, max(0.0, b_ratio))
    s_ratio = min(1.0, max(0.0, s_ratio))

    # 2. 撤单倾斜度: (撤买 - 撤卖) / 总撤单 (-1 ~ +1)
    cancel_bias = (b_cancel - s_cancel) / total_cancel

    # 3. 撤单活跃度: 撤单总笔数或总手在总委托中的占比
    if l2_order_num > 0:
        # 近似每笔委托对应撤单频率
        activity_score = min(1.0, total_cancel / (l2_order_num * 100.0))
    else:
        denom = (b_denom + s_denom)
        activity_score = min(1.0, total_cancel / denom) if denom > 0 else 0.0

    # 4. 结合广发金工与 A 股盘口实战的主力意图定性
    # - 虚假托盘 (Fake Bidding): 买方撤单极其显著 (cancel_bias > 0.25 且 b_ratio >= 0.35)
    # - 压盘吸筹 (Fake Selling): 卖方撤单极其显著 (cancel_bias < -0.25 且 s_ratio >= 0.35)
    chg = pct_chg if pct_chg is not None else 0.0

    intent_tag = "多空均衡"
    intent_desc = "买卖双方撤单频率处于常态区间，未见明显异常虚假挂撤单。"
    warning_level = "safe"

    if cancel_bias >= 0.25 and b_ratio >= 0.30:
        if chg > 2.0:
            intent_tag = "⚠️ 托单诱多"
            intent_desc = f"买盘撤单率高达 {b_ratio*100:.1f}%，显著高于卖盘，高位频现大托单撤销，谨防主力诱多出货。"
            warning_level = "warning"
        else:
            intent_tag = "买单虚脱"
            intent_desc = f"买盘撤单偏向显著 (偏向度 +{cancel_bias:.2f})，买方流动性承诺频繁撤回，支撑力度虚弱。"
            warning_level = "caution"
    elif cancel_bias <= -0.25 and s_ratio >= 0.30:
        if chg <= 0.5:
            intent_tag = "🛡️ 压盘吸筹"
            intent_desc = f"卖盘撤单率高达 {s_ratio*100:.1f}%，卖方大单频繁挂出又撤回，且股价未破位，疑似主力压单恐吓吸筹。"
            warning_level = "caution"
        else:
            intent_tag = "抛压减退"
            intent_desc = f"卖盘撤单偏向显著 (偏向度 {cancel_bias:.2f})，上方抛压单主动取消，空头阻力趋弱。"
            warning_level = "safe"
    elif b_ratio >= 0.45 and s_ratio >= 0.45:
        intent_tag = "频繁试盘"
        intent_desc = f"双向撤单率均处于异常高位 (买撤 {b_ratio*100:.1f}% / 卖撤 {s_ratio*100:.1f}%)，主力资金在微观盘口博弈剧烈。"
        warning_level = "caution"

    return {
        "available": True,
        "b_cancel": round(b_cancel, 2),
        "s_cancel": round(s_cancel, 2),
        "b_cancel_ratio": round(b_ratio, 4),
        "s_cancel_ratio": round(s_ratio, 4),
        "cancel_bias": round(cancel_bias, 4),
        "activity_score": round(activity_score, 4),
        "intent_tag": intent_tag,
        "intent_desc": intent_desc,
        "warning_level": warning_level,
    }
