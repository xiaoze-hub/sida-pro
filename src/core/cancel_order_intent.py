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

from typing import Any, Dict, Optional


def _bnum(baseline: Dict[str, Any] | None, *names: str) -> Optional[float]:
    """从基线字典取有限数值（兼容 profile.buy / 扁平两种形状）。"""
    if not isinstance(baseline, dict):
        return None
    for name in names:
        val = baseline.get(name)
        if val is None or isinstance(val, bool):
            continue
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if f == f and f not in (float("inf"), float("-inf")):
            return f
    return None


def baseline_coords(
    today_rate: Optional[float],
    baseline: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """今日撤单率 → 相对该股自身历史基线的 z 坐标 (I-3)。

    基线入参形状（两种都吃）:
        {"median": 0.41, "std": 0.09, "n": 59, "percentile": 97.0}
        {"b_median": .., "b_std": .., "b_n": ..}         # 扁平别名
    `std` 为 None/0（历史波动为零）或样本 < 20 时 z 为 None —— 不给伪 z。
    """
    out = {
        "available": False,
        "median": _bnum(baseline, "median", "b_median"),
        "std": _bnum(baseline, "std", "b_std"),
        "n": int(_bnum(baseline, "n", "b_n") or 0),
        "percentile": _bnum(baseline, "percentile", "b_percentile"),
        "z": None,
    }
    if today_rate is None or out["median"] is None or out["std"] is None:
        return out
    if out["n"] < 20 or out["std"] <= 1e-9:
        return out
    out["z"] = round((today_rate - out["median"]) / out["std"], 4)
    out["available"] = True
    return out


def _base_fields(coords: Dict[str, Any], basis: str = "fixed_threshold") -> Dict[str, Any]:
    """把基线坐标摊平成响应字段（所有返回分支共用同一 schema）。"""
    return {
        "decision_basis": basis,
        "baseline_available": coords["available"],
        "baseline_median": coords["median"],
        "baseline_std": coords["std"],
        "baseline_days": coords["n"],
        "zscore": coords["z"],
        "percentile": coords["percentile"],
    }



def calc_cancel_order_intent(
    more_info: Dict[str, Any] | None,
    pct_chg: float | None = None,
    baseline: Dict[str, Any] | None = None,
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
        baseline: I-3 委托行为画像的历史基线 `{median, std, n, percentile}`
            (由 `src.core.cancel_profile.build_cancel_profile` 的 `buy` 段产出)。
            **有基线时判定改用相对 z 坐标**, 固定 40% 阈值不再单独定性
            (同一阈值压 5000 只票必然一半误报, 见 docs/research 审计 I-3);
            无基线/样本不足时退回原固定阈值口径, 行为与历史版本一致。

    返回结构:
        {
            "available": bool,
            "b_cancel": float,           # 买盘撤单量(手)
            "s_cancel": float,           # 卖盘撤单量(手)
            "b_cancel_ratio": float,     # 买盘撤单率 (0~1)
            "s_cancel_ratio": float,     # 卖盘撤单率 (0~1)
            "cancel_bias": float,        # 撤单偏向 (-1~+1, >0 买方撤多, <0 卖方撤多)
            "activity_score": float,     # 撤单活跃度 (0~1)
            "intent_tag": str,           # 意图标签 (相对异常诱多/压盘吸筹/多空均衡/无数据)
            "intent_desc": str,          # 意图详解说明
            "warning_level": str,        # safe / caution / warning
            "decision_basis": str,       # baseline_z / fixed_threshold
            "baseline_available": bool,
            "baseline_median": float|None,
            "baseline_std": float|None,
            "baseline_days": int,
            "zscore": float|None,        # 今日撤买率相对自身基线的 z
            "percentile": float|None,    # 今日撤买率在基线中的分位 (P0~P100)
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
            **_base_fields(baseline_coords(None, baseline)),
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
            **_base_fields(baseline_coords(None, baseline)),
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

    # 4. I-3 动态基线坐标: 有历史基线时, 判定基准是"该股自己的分布", 不是全市场固定阈值
    coords = baseline_coords(b_ratio, baseline)
    z = coords["z"]
    chg = pct_chg if pct_chg is not None else 0.0

    intent_tag = "多空均衡"
    intent_desc = "买卖双方撤单频率处于常态区间，未见明显异常虚假挂撤单。"
    warning_level = "safe"
    basis = "fixed_threshold"

    if coords["available"] and z is not None:
        basis = "baseline_z"
        pct_txt = f"P{coords['percentile']:.0f}" if coords["percentile"] is not None else "P--"
        base_txt = (
            f"今日买撤 {b_ratio*100:.1f}%，为近 {coords['n']} 日 {pct_txt}"
            f"（常态 {coords['median']*100:.1f}%±{coords['std']*100:.1f}pp，z={z:+.2f}）"
        )
        # 相对口径三分支: 只有"相对它自己异常"才报警; |z|<1 一律常态(不误报)
        if z >= 2.0 and chg > 3.0:
            intent_tag = "⚠️ 相对异常诱多"
            intent_desc = base_txt + "，撤单抬升与拉升同步，相对该股自身历史异常，谨防托单诱多出货。"
            warning_level = "warning"
        elif z <= -1.0 and chg < -2.0:
            intent_tag = "抛压真实确认"
            intent_desc = base_txt + "，撤单萎缩伴随股价破位，挂单被真实成交，抛压非虚挂。"
            warning_level = "caution"
        elif abs(z) < 1.0:
            intent_tag = "常态"
            intent_desc = base_txt + "，处于该股自身常态区间（固定 40% 阈值口径在此不适用，不构成异常）。"
            warning_level = "safe"
        elif z >= 2.0:
            intent_tag = "撤单抬升·涨幅未配合"
            intent_desc = base_txt + "，撤单显著高于自身常态但涨幅未达诱多确认条件，仅作观察。"
            warning_level = "caution"
        elif z <= -1.0:
            intent_tag = "撤单显著偏低"
            intent_desc = base_txt + "，撤单显著低于自身常态，挂单意愿稳定或挂单被成交消化。"
            warning_level = "safe"
        else:
            intent_tag = "轻度偏离"
            intent_desc = base_txt + "，偏离常态但未达异常阈值。"
            warning_level = "safe"

    # 5. 无基线（或基线样本不足）时退回固定阈值口径 —— 行为与历史版本一致
    # - 虚假托盘 (Fake Bidding): 买方撤单极其显著 (cancel_bias > 0.25 且 b_ratio >= 0.30)
    # - 压盘吸筹 (Fake Selling): 卖方撤单极其显著 (cancel_bias < -0.25 且 s_ratio >= 0.30)
    elif cancel_bias >= 0.25 and b_ratio >= 0.30:
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
        **_base_fields(coords, basis),
    }
