"""情景规则引擎（2026-10-21）。

替掉"预案靠模型自由发挥"：把 关键位 + 筹码带 + 均线 + 量能 + 因子位置 折算成
**三档情景（走强 / 震荡 / 走弱）**，每档写「触发条件 → 关键位 → 应对」，并给失效条件。

纪律：
- 不预测方向，只给"条件-反应"（用户已明确 AI 方向预测不可靠）；
- 关键位全部由数据算出（均线、筹码带、近 20/120 日高低温），缺失就不写该条；
- 输出固定三档，不因数据好坏增删档位。
"""
from __future__ import annotations

from typing import Any


def _levels(trend: dict, chips: dict, intraday: dict | None = None) -> dict[str, Any]:
    """阻力必须**在现价之上**、支撑必须**在现价之下**。

    2026-10-23 审计修复两处：
    1. 原实现 `up = [v for v in up if v > price] or up` —— 现价创区间新高时 up 全被
       过滤掉，`or up` 又把**低于现价**的值放回去，于是"阻力 32.5 < 现价 34"，
       走强的触发条件变成"站上 32.5"（其实早已在下方），整档情景失真。
       现在无上方位就 `resist=None` + `resist_state="new_high"`，按事实说话。
    2. MA60/MA20 只在各自固定的一侧候选里：股价跌破 MA60 时 MA60 本是**最近阻力**，
       却被丢进 support 候选再被 `v < price` 过滤掉，白白丢失。
       现在统一入池，按现价统一分上下。
    """
    mas = trend.get("mas") or {}
    price = trend.get("last") or (intraday or {}).get("last")
    band_hi = chips.get("band70_hi")
    band_lo = chips.get("band70_lo")
    pool = [v for v in (mas.get(20), mas.get(60), band_hi, band_lo,
                        trend.get("high"), trend.get("low")) if v]
    above = [v for v in pool if price and v > price]
    below = [v for v in pool if price and v < price]
    resist = round(min(above), 2) if above else None
    support = round(max(below), 2) if below else None
    return {
        "price": price,
        "resist": resist,
        "support": support,
        "resist_state": "new_high" if (price and not resist) else "normal",
        "ma20": mas.get(20),
        "ma60": mas.get(60),
        "band_hi": band_hi,
        "band_lo": band_lo,
        "vwap": (intraday or {}).get("vwap"),
    }


def build_scenarios(trend: dict, chips: dict, factors: dict | None = None,
                    intraday: dict | None = None) -> dict[str, Any]:
    """纯计算：输入各维指标，输出三档情景。"""
    lv = _levels(trend, chips, intraday)
    # 现价创新高时没有"上方位"（resist=None），这是事实而不是缺数据 ——
    # 只要还有支撑位就照样给三档（走强的触发改为"不破支撑 + 量能确认"）。
    if lv["resist"] is None and lv["support"] is None:
        return {"available": False, "reason": "关键位不足（缺均线/筹码带/区间高低）"}

    vol_ratio = (intraday or {}).get("vol_ratio") or trend.get("vol_ratio")
    pos = trend.get("position")
    rs = trend.get("rs20")
    mom20 = (factors or {}).get("values", {}).get("momentum_20")

    new_high = lv["resist"] is None
    resist_ref = lv["resist"] if not new_high else (lv["band_hi"] or trend.get("high"))
    # 走弱目标不能等于触发位（"跌破支撑、目标就是支撑"是循环表述）
    below = [v for v in (chips.get("band70_lo"), trend.get("low")) if v and lv["support"] and v < lv["support"]]
    weak_target = max(below) if below else None

    if new_high:
        strong_trigger = (f"已站上全部已识别阻力（前高 {resist_ref}，区间新高）："
                          f"收盘不破支撑 {lv['support']} 且当日量能 ≥1.2×20 日均量")
        strong_target = "上方无已计算阻力（新高区，逐日重算）"
        range_trigger = f"在 {lv['support']} ~ 前高 {resist_ref} 之间震荡且量能中性"
        range_target = f"{lv['support']} ~ {resist_ref}"
        weak_trigger = f"收盘跌破 {lv['support']} 且放量（量能 ≥1.2× 均量）"
    else:
        strong_trigger = f"收盘站上 {lv['resist']} 且当日量能 ≥1.2×20 日均量"
        strong_target = lv["band_hi"] or lv["resist"]
        range_trigger = f"在 {lv['support']} ~ {lv['resist']} 之间震荡且量能中性"
        range_target = f"{lv['support']} ~ {lv['resist']}"
        weak_trigger = f"收盘跌破 {lv['support']} 且放量（量能 ≥1.2× 均量）"

    return {
        "available": True,
        "levels": lv,
        "context": {"position": pos, "rs20": rs, "momentum_20": mom20, "vol_ratio": vol_ratio},
        "scenarios": [
            {
                "name": "走强",
                "trigger": strong_trigger,
                "target": strong_target,
                "response": ("已在新高区：不追高，等回踩不破 "
                             + str(resist_ref) + " 再视强弱") if new_high else
                            ("放量突破后回踩不破 " + str(lv["resist"]) + " 可视为有效；追高需量能确认"),
                "invalid": ("回落至 " + str(resist_ref) + " 下方且缩量 → 视为假突破") if new_high else
                           ("突破当日即回落至 " + str(lv["resist"]) + " 下方且缩量 → 视为假突破"),
            },
            {
                "name": "震荡",
                "trigger": range_trigger,
                "target": range_target,
                "response": "区间内以均价线/VWAP 为多空分界，靠近上沿减、靠近下沿观察承接",
                "invalid": "任一端被有效突破（收盘价 + 量能确认）→ 切换走强/走弱",
            },
            {
                "name": "走弱",
                "trigger": weak_trigger,
                "target": weak_target if weak_target is not None else "下方无明确支撑（需重新评估）",
                "response": "跌破后反抽不过 " + str(lv["support"]) + " 视为破位确认；按纪律减仓",
                "invalid": "跌破后次日快速收回 " + str(lv["support"]) + " → 视为挖坑",
            },
        ],
        "disclaimer": "不预测方向，只给条件-反应；关键位由均线/筹码带/区间高低算出，非主观画线。",
    }


def render(m: dict) -> str:
    if not m.get("available"):
        return f"[情景矩阵] {m.get('reason', '关键位不足')}——不硬编情景。"
    lv, ctx = m["levels"], m["context"]
    _r = lv.get("resist")
    _s = lv.get("support")
    lines = [
        "[数据源: 均线 + CYQ 筹码带 + 区间高低 + 量能/因子]",
        "关键位：阻力 {}（上方候选取最近；现价创新高时无上方位）｜支撑 {}（下方候选取最近）".format(
            (_r if _r is not None else "无（区间新高）"),
            (_s if _s is not None else "无（区间新低）")),
        "位置参考：日内区间位置 {p}%｜20 日相对强弱 {rs}｜20 日动量 {m20}｜量比 {vr}".format(
            p=ctx.get("position"), rs=("--" if ctx.get("rs20") is None else f"{ctx['rs20']:+.2f}pp"),
            m20=("--" if ctx.get("momentum_20") is None else f"{ctx['momentum_20']:+.2f}%"),
            vr=("--" if ctx.get("vol_ratio") is None else ctx["vol_ratio"]),
        ),
    ]
    for sc in m["scenarios"]:
        lines.append(f"【{sc['name']}】触发：{sc['trigger']}｜目标：{sc['target']}｜应对：{sc['response']}")
        lines.append(f"　失效：{sc['invalid']}")
    lines.append(m["disclaimer"])
    return "\n".join(lines)


def build_matrix(db, symbol: str, market: str = "CN") -> dict[str, Any]:
    """结构化入口（给 HTTP 端点/前端画线用）：返回 {available, levels, scenarios, ...}。

    与 `build()` 同一套计算，只是一个返回 dict、一个返回人话文本 —— 不许出现第二套口径。
    """
    try:
        from src.core.stock_factors import load as load_factors
        from src.core.trend_profile import compute as compute_trend
        from src.db.klines_repo import load_qfq_bars_cached as load_qfq_bars

        bars = load_qfq_bars(symbol, market, days=250)
        trend = compute_trend(bars, None)
        if not trend.get("available"):
            return {"available": False, "reason": trend.get("reason") or "日线不足"}

        chips: dict[str, Any] = {}
        try:
            from src.core.chip_distribution import compute_chips

            cyq = compute_chips(bars[-120:]) or {}
            chips = {
                "band70_lo": cyq.get("cost_10"),
                "band70_hi": cyq.get("cost_90"),
                "peak_price": cyq.get("peak_price"),
            }
        except Exception:  # noqa: BLE001
            chips = {}

        factors = load_factors(db, symbol, market)
        matrix = build_scenarios(trend, chips, factors)
        if matrix.get("available"):
            matrix["price"] = trend.get("price")
            matrix["as_of"] = trend.get("as_of")
        return matrix
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"[:120]}


def build(db, symbol: str, market: str = "CN") -> str:
    """工具入口：从现有模块取数后算情景。缺哪块就少哪块，不编。"""
    try:
        from src.core.stock_factors import load as load_factors
        from src.core.trend_profile import compute as compute_trend
        from src.db.klines_repo import load_qfq_bars_cached as load_qfq_bars

        bars = load_qfq_bars(symbol, market, days=250)
        trend = compute_trend(bars, None)
        if not trend.get("available"):
            return f"[情景矩阵] {trend.get('reason', '日线不足')}——不硬编情景。"

        chips: dict[str, Any] = {}
        try:
            from src.core.chip_distribution import compute_chips

            cyq = compute_chips(bars[-120:]) or {}
            chips = {
                "band70_lo": cyq.get("cost_10"),
                "band70_hi": cyq.get("cost_90"),
                "peak_price": cyq.get("peak_price"),
            }
        except Exception:  # noqa: BLE001
            chips = {}

        factors = load_factors(db, symbol, market)
        return render(build_matrix(db, symbol, market))
    except Exception as exc:  # noqa: BLE001
        return f"[情景矩阵] 计算失败：{type(exc).__name__}——不硬编情景。"
