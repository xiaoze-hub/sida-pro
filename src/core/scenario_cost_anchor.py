"""持仓成本锚定情景（2026-10-23）。

为什么需要它：`scenario_engine` 对所有用户算同一组 support / resist。但对**持有该股**的人，
支撑位不是「参考位」，而是「我的成本线 / 我的纪律止损线」——同一根 K 线，空仓和满仓的
含义完全不同。本模块把「本人成本 + 本人纪律止损」叠进情景矩阵，输出本人视角的
解套位 / 保本位 / 纪律位，全部数字来自矩阵，一个都不新造。

纪律（硬）：
- 纯函数：返回**新 dict**，绝不改传入的 matrix（下游可能在同一轮里复用原矩阵）；
- 没有成本（未上传交割单 / 无该股持仓）就写「不假设你的成本」，不按现价反推成本；
- 没有纪律止损就**不出现**纪律位，绝不默认 -5% / -8% 这类行业惯例；
- 只做位置换算，不给买卖建议。
"""
from __future__ import annotations

import copy
from typing import Any

NO_COST_REASON = "无持仓成本（未上传交割单或无该股持仓）"
ZONES = ("套牢区", "成本区间", "安全垫区", "破位区")


def _f(v: Any) -> float | None:
    """数值化。非数/NaN/Inf 当「没取到」，不拿 0 冒充。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


def _pos(v: Any) -> float | None:
    x = _f(v)
    return x if (x is not None and x > 0) else None


def _classify(price: float, cost: float, support: float, resist: float) -> str:
    """区域判定：**顺序即优先级**，破位优先（跌破支撑比套牢更急）。

    - 破位区：现价 < 支撑（结构先坏了，成本在哪都次要）
    - 套牢区：现价 < 成本 且 成本在阻力之上（上有阻力、下有成本，回本要跨两道）
    - 安全垫区：现价 > 成本 且 成本 ≥ 支撑（成本已被支撑保护）
      以及「成本在支撑下方」（浮盈更厚）——同属有垫，不另立标签
    - 成本区间：现价与成本都落在支撑~阻力之间
    """
    if price < support:
        return "破位区"
    if price < cost and cost > resist:
        return "套牢区"
    if price > cost and cost >= support:
        return "安全垫区"
    if support <= price <= resist and support <= cost <= resist:
        return "成本区间"
    if price > cost:                     # 成本在支撑下方：垫更厚，仍是安全垫
        return "安全垫区"
    return "成本区间"                     # 现价 ≤ 成本且成本未越过阻力：都在区间内


def _pct_at(level: float, cost: float) -> float:
    """价格 level 相对本人成本的浮动盈亏（%）。"""
    return (level - cost) / cost * 100


def _levels_for_owner(price: float, cost: float, support: float, resist: float,
                      stop: float | None) -> list[dict[str, Any]]:
    """把矩阵里的关键位翻成「对我这个持仓意味着什么」。数字全部来自入参。"""
    out: list[dict[str, Any]] = [
        {
            "label": "成本",
            "price": cost,
            "meaning": f"持仓成本 {cost:.2f}（保本位，未计手续费）｜现价距成本 "
                       f"{_pct_at(price, cost):+.2f}%",
        },
    ]
    sp = _pct_at(support, cost)
    # 已经破位就说「已跌破」，别把既成事实写成待触发条件。
    s_verb = "已跌破" if price < support else "跌破"
    out.append({
        "label": "支撑",
        "price": support,
        "meaning": (f"价格回到 {support:.2f} → 你仍有浮盈 {sp:+.2f}%" if sp >= 0
                    else f"{s_verb} {support:.2f} → 你的浮亏扩大到 {sp:+.2f}%"),
    })
    rp = _pct_at(resist, cost)
    r_verb = "已站上" if price > resist else "站上"
    out.append({
        "label": "阻力",
        "price": resist,
        "meaning": (f"{r_verb} {resist:.2f} → 你的浮盈扩大到 {rp:+.2f}%" if rp >= 0
                    else f"{r_verb} {resist:.2f} → 你的浮亏收窄到 {rp:+.2f}%"),
    })
    if stop is not None:
        dp = _pct_at(stop, cost)
        out.append({
            "label": "纪律止损",
            "price": stop,
            "meaning": (f"跌破 {stop:.2f} → 你的浮盈收窄至 {dp:+.2f}%（本人纪律位）" if dp >= 0
                        else f"跌破 {stop:.2f} → 你的浮亏扩大到 {dp:+.2f}%（本人纪律位）"),
        })
    return out


def enrich_with_cost(matrix: dict, *, cost_price: float | None,
                     stop_price: float | None = None) -> dict[str, Any]:
    """把持仓成本 / 纪律止损叠进情景矩阵，返回**新 dict**（入参只读）。

    返回原矩阵的全部键 + `cost_anchor`：
    {available, cost_price, stop_price, zone, distance_to_support_pct,
     distance_to_cost_pct, break_even_price, levels_for_owner}
    """
    out: dict[str, Any] = copy.deepcopy(matrix) if isinstance(matrix, dict) else {}
    cost = _pos(cost_price)
    if cost is None:
        out["cost_anchor"] = {"available": False, "reason": NO_COST_REASON}
        return out

    raw_levels = out.get("levels")
    levels: dict[str, Any] = raw_levels if isinstance(raw_levels, dict) else {}
    price = _pos(levels.get("price")) or _pos(out.get("price"))
    support = _pos(levels.get("support"))
    resist = _pos(levels.get("resist"))
    if price is None or support is None or resist is None:
        out["cost_anchor"] = {
            "available": False,
            "reason": "情景矩阵缺关键位（现价/支撑/阻力未算出）——不做成本锚定。",
        }
        return out

    stop = _pos(stop_price)
    zone = _classify(price, cost, support, resist)

    out["cost_anchor"] = {
        "available": True,
        "cost_price": cost,
        "stop_price": stop,
        "price": price,
        "zone": zone,
        # 现价相对支撑：负值 = 已跌到支撑下方（破位）
        "distance_to_support_pct": round((price - support) / support * 100, 2),
        # 现价相对成本 = 本人浮动盈亏（%）
        "distance_to_cost_pct": round((price - cost) / cost * 100, 2),
        "break_even_price": cost,        # 保本价 = 成本本身（未计费用，不编额外费率）
        "levels_for_owner": _levels_for_owner(price, cost, support, resist, stop),
    }
    return out


def render_cost_anchor(anchor: dict) -> str:
    """3~6 行纯文本；不可用只有一句「不假设你的成本」。"""
    if not anchor or not anchor.get("available"):
        return "[成本锚定] 无持仓成本——不假设你的成本。"

    stop = anchor.get("stop_price")
    head = (
        f"[持仓成本锚定] 成本 {anchor['cost_price']:.2f}（保本位）"
        + (f"｜纪律止损 {stop:.2f}" if stop is not None else "｜未设纪律位")
        + f"｜现价 {anchor['price']:.2f}"
        + f"｜距成本 {anchor['distance_to_cost_pct']:+.2f}%"
        + f"｜距支撑 {anchor['distance_to_support_pct']:+.2f}%"
        + f"｜区域：{anchor['zone']}"
    )
    lines = [head]
    for lv in anchor.get("levels_for_owner") or []:
        if lv.get("label") == "成本":
            continue                     # 成本已在首行，别占一行
        lines.append(f"  {lv['label']} {lv['price']:.2f} —— {lv['meaning']}")
    lines.append("以上为关键位换算（非买卖建议）。")
    return "\n".join(lines)
