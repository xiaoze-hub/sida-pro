"""决策合成(2026-09-08 方向2): 三信号 → 动手/看看/别碰 + 一行理由。

输入复用现有能力, 不重写算法:
- 趋势: gs_strategy.eval_gs + trend_label
- 活跃度: ai_activity.eval_activity(当日 + 砍末根算前日)
- 资金: dark_pool_flow.compute_pool_flow(symbol).main_net(明+暗)
- 合成: resonance.evaluate_state(7 行状态表) → verdict 映射

verdict 映射: 向好→动手, 拐点/分歧→看看, 走坏→别碰;
缺数/表外(row=0)→看看(理由写清缺什么, 不编造方向)。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_VERDICT_BY_PHASE = {
    "向好": "动手",
    "拐点": "看看",
    "分歧": "看看",
    "走坏": "别碰",
}


def synthesize(
    trend: str | None,
    activity: float | None,
    activity_prev: float | None,
    fund_net: float | None,
    fund_net_prev: float | None = None,
) -> dict:
    """纯函数: 三信号 → {verdict, reason, parts}。无 IO, 可单测。"""
    from src.core.resonance import evaluate_state

    st = evaluate_state(
        trend=trend or "无数据",
        activity=activity,
        activity_prev=activity_prev,
        fund_net=fund_net,
        fund_net_prev=fund_net_prev,
    )
    phase = st.get("phase") or "无"
    if phase == "无" or st.get("row", 0) == 0:
        missing = st.get("note") or "信号不全"
        return {
            "verdict": "看看",
            "reason": f"信号不全({missing}), 先别动手",
            "phase": phase,
            "row": st.get("row", 0),
            "parts": _parts_text(trend, activity, fund_net),
        }
    verdict = _VERDICT_BY_PHASE.get(phase, "看看")
    return {
        "verdict": verdict,
        "reason": _reason_line(verdict, trend, activity, activity_prev, fund_net, st),
        "phase": phase,
        "row": st.get("row", 0),
        "parts": _parts_text(trend, activity, fund_net),
    }


def _parts_text(trend, activity, fund_net) -> dict:
    return {
        "trend": trend or "无数据",
        "activity": activity,
        "fund_net": fund_net,
    }


def _reason_line(verdict, trend, activity, activity_prev, fund_net, st) -> str:
    bits = [f"趋势{trend}"]
    if activity is None:
        bits.append("活跃度无数据")
    else:
        s = f"活跃度{activity:.2f}"
        if activity_prev is not None and activity_prev > 0 and activity >= 2 * activity_prev:
            s += "(较前日翻倍)"
        bits.append(s)
    if fund_net is None:
        bits.append("主力无数据")
    else:
        bits.append(f"主力{'流入' if fund_net > 0 else '流出'}{abs(fund_net) / 1e8:.2f}亿")
    return f"{verdict}: {'、'.join(bits)}"


def decide(symbol: str, market: str = "CN", days: int = 120) -> dict:
    """IO 入口: 拉 K 线 → 三信号 → synthesize。失败一律看看 + 理由, 不抛。"""
    try:
        from src.collectors.kline_collector import KlineCollector
        from src.models.market import MarketCode
        from src.core.gs_strategy import eval_gs, trend_label
        from src.core.ai_activity import eval_activity
        from src.core.dark_pool_flow import compute_pool_flow

        mc = MarketCode(market)
        klines = KlineCollector(mc).get_klines(symbol, days=days)
        bars = [
            {"date": k.date, "open": k.open, "close": k.close,
             "high": k.high, "low": k.low, "volume": k.volume}
            for k in (klines or [])
        ]
        if not bars:
            return {"symbol": symbol, "verdict": "看看",
                    "reason": "看看: 无 K 线数据, 先别动手", "phase": "无", "row": 0,
                    "parts": _parts_text(None, None, None)}
        trend = trend_label(eval_gs(bars))
        act = eval_activity(bars)
        activity = act.get("activity") if isinstance(act, dict) else None
        act_prev = eval_activity(bars[:-1])
        activity_prev = act_prev.get("activity") if isinstance(act_prev, dict) else None
        try:
            fund_net = compute_pool_flow(symbol).main_net
        except Exception as e:  # noqa: BLE001
            logger.debug("decision fund %s failed: %s", symbol, e)
            fund_net = None
        out = synthesize(trend, activity, activity_prev, fund_net)
        out["symbol"] = symbol
        return out
    except Exception as e:  # noqa: BLE001 - 决策口永不 500
        logger.warning("decision %s failed: %s", symbol, e)
        return {"symbol": symbol, "verdict": "看看",
                "reason": f"看看: 计算失败({type(e).__name__}), 先别动手",
                "phase": "无", "row": 0, "parts": _parts_text(None, None, None)}
