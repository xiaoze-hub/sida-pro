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

    第一版直接对候选取 min/max，结果 MA60(35.8) 被当成支撑、却比现价(33)还高 ——
    "支撑在阻力上方"是逻辑错误，不是数据问题。这里按现价过滤后再取最近。
    """
    mas = trend.get("mas") or {}
    price = trend.get("last") or (intraday or {}).get("last")
    band_hi = chips.get("band70_hi")
    band_lo = chips.get("band70_lo")
    up = [v for v in (mas.get(20), band_hi, trend.get("high")) if v]
    down = [v for v in (mas.get(60), band_lo, trend.get("low")) if v]
    if price:
        up = [v for v in up if v > price] or up
        down = [v for v in down if v < price] or down
    return {
        "price": price,
        "resist": round(min(up), 2) if up else None,
        "support": round(max(down), 2) if down else None,
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
    if not lv["resist"] or not lv["support"]:
        return {"available": False, "reason": "关键位不足（缺均线/筹码带/区间高低）"}

    vol_ratio = (intraday or {}).get("vol_ratio") or trend.get("vol_ratio")
    pos = trend.get("position")
    rs = trend.get("rs20")
    mom20 = (factors or {}).get("values", {}).get("momentum_20")

    # 走弱目标不能等于触发位（"跌破支撑、目标就是支撑"是循环表述）
    below = [v for v in (chips.get("band70_lo"), trend.get("low")) if v and lv["support"] and v < lv["support"]]
    weak_target = max(below) if below else None
    strong_trigger = f"收盘站上 {lv['resist']} 且当日量能 ≥1.2×20 日均量"
    weak_trigger = f"收盘跌破 {lv['support']} 且放量（量能 ≥1.2× 均量）"
    range_trigger = f"在 {lv['support']} ~ {lv['resist']} 之间震荡且量能中性"

    return {
        "available": True,
        "levels": lv,
        "context": {"position": pos, "rs20": rs, "momentum_20": mom20, "vol_ratio": vol_ratio},
        "scenarios": [
            {
                "name": "走强",
                "trigger": strong_trigger,
                "target": lv["band_hi"] or lv["resist"],
                "response": "放量突破后回踩不破 " + str(lv["resist"]) + " 可视为有效；追高需量能确认",
                "invalid": "突破当日即回落至 " + str(lv["resist"]) + " 下方且缩量 → 视为假突破",
            },
            {
                "name": "震荡",
                "trigger": range_trigger,
                "target": f"{lv['support']} ~ {lv['resist']}",
                "response": "区间内以均价线/VWAP 为多空分界，靠近上沿减、靠近下沿观察承接",
                "invalid": f"任一端被有效突破（收盘价 + 量能确认）→ 切换走强/走弱",
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
    lines = [
        "[数据源: 均线 + CYQ 筹码带 + 区间高低 + 量能/因子]",
        "关键位：阻力 {r}（MA20/筹码带上沿/区间高取最近）｜支撑 {s}（MA60/筹码带下沿/区间低取最近）".format(
            r=lv["resist"], s=lv["support"]),
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


def build(db, symbol: str, market: str = "CN") -> str:
    """工具入口：从现有模块取数后算情景。缺哪块就少哪块，不编。"""
    try:
        from src.core.stock_factors import load as load_factors
        from src.core.trend_profile import compute as compute_trend
        from src.db.klines_repo import load_qfq_bars

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
        return render(build_scenarios(trend, chips, factors))
    except Exception as exc:  # noqa: BLE001
        return f"[情景矩阵] 计算失败：{type(exc).__name__}——不硬编情景。"
