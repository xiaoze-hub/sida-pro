"""src/core/copilot_brief.py

AI 数智参谋（Copilot）后端判定引擎与证据聚合层。
契约源: docs/research/AI数智参谋全链路落地设计方案.md §4
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from src.core.trade_phase import phase_meta

logger = logging.getLogger(__name__)

Tone = Literal["opportunity", "neutral", "caution", "risk"]
DefenseSlot = Literal["first", "core", "probe", "cost", "cap"]

DISCLAIMER = "客观证据 · 仅供参考 · 不构成投资建议与买卖指令"


def _now_cst_iso() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 证据取数适配器
# ---------------------------------------------------------------------------

def _fetch_quote_evidence(symbol: str, market: str) -> dict:
    from src.core.marketdata_client import md_quote_rows
    try:
        rows = md_quote_rows([symbol], market)
        if rows and isinstance(rows, list) and len(rows) > 0:
            r = rows[0]
            return {
                "available": True,
                "close": _safe_float(r.get("close")),
                "change_pct": _safe_float(r.get("change_pct")),
                "prev_close": _safe_float(r.get("prev_close")),
                "open": _safe_float(r.get("open")),
                "high": _safe_float(r.get("high")),
                "low": _safe_float(r.get("low")),
                "source": r.get("source", "marketdata"),
                "as_of": _now_cst_iso(),
                "degraded_reason": None,
            }
        elif isinstance(rows, dict) and symbol in rows:
            r = rows[symbol]
            return {
                "available": True,
                "close": _safe_float(r.get("close")),
                "change_pct": _safe_float(r.get("change_pct")),
                "prev_close": _safe_float(r.get("prev_close")),
                "open": _safe_float(r.get("open")),
                "high": _safe_float(r.get("high")),
                "low": _safe_float(r.get("low")),
                "source": r.get("source", "marketdata"),
                "as_of": _now_cst_iso(),
                "degraded_reason": None,
            }
    except Exception as e:
        logger.warning("Quote fetch error for %s: %s", symbol, e)
    return {
        "available": False,
        "close": None,
        "change_pct": None,
        "prev_close": None,
        "open": None,
        "high": None,
        "low": None,
        "source": None,
        "as_of": None,
        "degraded_reason": "行情数据暂时不可用",
    }


def _fetch_chips_evidence(symbol: str, market: str) -> dict:
    from src.core.chip_distribution import compute_main_accumulation_chips
    from src.core.decision_pioneer import fetch_bars
    caliber = "自算CYQ(120日·三角分布+换手衰减)，底仓成本=COST(50)"
    try:
        bars = fetch_bars(symbol, market, 140)
        if bars and len(bars) >= 20:
            c = compute_main_accumulation_chips(bars, lookback_days=120)
            if c:
                return {
                    "available": True,
                    "caliber": caliber,
                    "as_of": _now_cst_iso(),
                    "degraded_reason": None,
                    "gated": False,
                    "main_cost": _safe_float(c.get("main_cost")),
                    "peak_price": _safe_float(c.get("peak_price")),
                    "peak_ratio": _safe_float(c.get("peak_ratio")),
                    "cost_70_low": _safe_float(c.get("cost_70_low")),
                    "cost_70_high": _safe_float(c.get("cost_70_high")),
                    "cost_90_low": _safe_float(c.get("cost_90_low")),
                    "cost_90_high": _safe_float(c.get("cost_90_high")),
                    "profit_ratio": _safe_float(c.get("profit_ratio")),
                    "profit_state": c.get("profit_state"),
                    "concentration": _safe_float(c.get("concentration")),
                    "pattern": c.get("pattern"),
                    "status_label": c.get("status_label"),
                    "signal_level": c.get("signal_level"),
                    "departure_pct": _safe_float(c.get("departure_pct")),
                    "lookback_days": _safe_int(c.get("lookback_days")),
                }
    except Exception as e:
        logger.warning("Chips evidence error for %s: %s", symbol, e)
    return {
        "available": False,
        "caliber": caliber,
        "as_of": None,
        "degraded_reason": "K线或历史成交量样本不足",
        "gated": False,
        "main_cost": None,
        "peak_price": None,
        "peak_ratio": None,
        "cost_70_low": None,
        "cost_70_high": None,
        "cost_90_low": None,
        "cost_90_high": None,
        "profit_ratio": None,
        "profit_state": None,
        "concentration": None,
        "pattern": None,
        "status_label": None,
        "signal_level": None,
        "departure_pct": None,
        "lookback_days": None,
    }


def _fetch_resonance_evidence(symbol: str, market: str) -> dict:
    from src.core.decision_pioneer import fetch_decision_pioneer
    caliber = "GS+AI机构活跃度+L2主力净额(TQ口径)，五态状态机"
    try:
        dp = fetch_decision_pioneer(symbol, market)
        if dp and isinstance(dp, dict):
            act = dp.get("institution_activity") or {}
            gs = dp.get("gs") or {}
            l2 = dp.get("l2") or {}
            return {
                "available": True,
                "caliber": caliber,
                "as_of": _now_cst_iso(),
                "degraded_reason": None,
                "gated": False,
                "gs_zone": gs.get("zone"),
                "gs_signal": gs.get("signal"),
                "gs_signal_date": gs.get("signal_date"),
                "gs_pending": gs.get("pending"),
                "activity": _safe_float(act.get("activity")),
                "activity_level": act.get("level"),
                "activity_above_strong": act.get("above_strong"),
                "activity_streak_days": _safe_int(act.get("streak_days")),
                "activity_ma5": _safe_float(act.get("ma5")),
                "l2_net_wan": _safe_float(l2.get("zjl_hb")),
                "l2_direction": l2.get("direction"),
                "l2_cancel_rate": _safe_float(l2.get("cancel_rate")),
                "l2_cancel_bias": _safe_float(l2.get("cancel_bias")),
                "resonance_state": dp.get("resonance_state"),
                "resonance_action": dp.get("resonance_action"),
                "resonance_warning": dp.get("resonance_warning"),
            }
    except Exception as e:
        logger.warning("Resonance evidence error for %s: %s", symbol, e)
    return {
        "available": False,
        "caliber": caliber,
        "as_of": None,
        "degraded_reason": "决策先锋数据未返回",
        "gated": False,
        "gs_zone": None,
        "gs_signal": None,
        "gs_signal_date": None,
        "gs_pending": None,
        "activity": None,
        "activity_level": None,
        "activity_above_strong": None,
        "activity_streak_days": None,
        "activity_ma5": None,
        "l2_net_wan": None,
        "l2_direction": None,
        "l2_cancel_rate": None,
        "l2_cancel_bias": None,
        "resonance_state": None,
        "resonance_action": None,
        "resonance_warning": None,
    }


def _fetch_micro_evidence(symbol: str, market: str, quote: dict) -> dict:
    from src.core.cancel_order_intent import calc_cancel_order_intent
    from src.core.marketdata_client import md_more_info
    caliber = "TQ会话撤单量 BCancel/(TotalBVol+BCancel)；收盘回退最近快照"
    try:
        rows = md_more_info([symbol], market)
        info = rows[0] if (rows and isinstance(rows, list)) else {}
        pct_chg = quote.get("change_pct") or 0.0
        # 字段映射
        raw = {
            "BCancel": info.get("b_cancel") or info.get("BCancel") or 0,
            "SCancel": info.get("s_cancel") or info.get("SCancel") or 0,
            "TotalBVol": info.get("total_b_vol") or info.get("TotalBVol") or 0,
            "TotalSVol": info.get("total_s_vol") or info.get("TotalSVol") or 0,
        }
        res = calc_cancel_order_intent(raw, pct_chg=pct_chg)
        return {
            "available": True,
            "caliber": caliber,
            "as_of": _now_cst_iso(),
            "degraded_reason": None,
            "gated": False,
            "b_cancel_ratio": _safe_float(res.get("b_cancel_ratio")),
            "s_cancel_ratio": _safe_float(res.get("s_cancel_ratio")),
            "cancel_bias": _safe_float(res.get("cancel_bias")),
            "activity_score": _safe_float(res.get("activity_score")),
            "intent_tag": res.get("intent_tag"),
            "intent_desc": res.get("intent_desc"),
            "warning_level": res.get("warning_level"),
            "volume_ratio": _safe_float(info.get("volume_ratio")),
            "turnover_rate": _safe_float(info.get("turnover_rate")),
            "session_fallback": bool(res.get("is_snapshot_fallback", False)),
        }
    except Exception as e:
        logger.warning("Micro evidence error for %s: %s", symbol, e)
    return {
        "available": False,
        "caliber": caliber,
        "as_of": None,
        "degraded_reason": "撤单流水暂无更新",
        "gated": False,
        "b_cancel_ratio": None,
        "s_cancel_ratio": None,
        "cancel_bias": None,
        "activity_score": None,
        "intent_tag": None,
        "intent_desc": None,
        "warning_level": None,
        "volume_ratio": None,
        "turnover_rate": None,
        "session_fallback": False,
    }


def _fetch_wyckoff_evidence(symbol: str, market: str) -> dict:
    from src.core.decision_pioneer import fetch_bars
    from src.core.wyckoff import analyze_wyckoff
    caliber = "日K量价结构(放量1.8×/缩量0.6×/弹簧回收≥1%)，不含逐笔"
    try:
        bars = fetch_bars(symbol, market, 90)
        if bars and len(bars) >= 15:
            res = analyze_wyckoff(bars, lookback=60)
            verdict = res.get("verdict") or {}
            markers = res.get("markers") or []
            spring_low = None
            for m in reversed(markers):
                if m.get("code") == "spring_probe":
                    idx = m.get("index")
                    if idx is not None and 0 <= idx < len(bars):
                        b = bars[idx]
                        spring_low = b.get("low") if isinstance(b, dict) else getattr(b, "low", None)
                    break
            return {
                "available": True,
                "caliber": caliber,
                "as_of": _now_cst_iso(),
                "degraded_reason": None,
                "gated": False,
                "stage": verdict.get("stage"),
                "stage_label": verdict.get("stage_label"),
                "effort_result": verdict.get("effort_result"),
                "markers_count": _safe_int(verdict.get("markers_count", len(markers))),
                "recent": markers[-5:],
                "spring_low": _safe_float(spring_low),
                "note": "仅日K量价结构，非方向结论",
            }
    except Exception as e:
        logger.warning("Wyckoff evidence error for %s: %s", symbol, e)
    return {
        "available": False,
        "caliber": caliber,
        "as_of": None,
        "degraded_reason": "K线形态计算暂不可用",
        "gated": False,
        "stage": None,
        "stage_label": None,
        "effort_result": None,
        "markers_count": None,
        "recent": [],
        "spring_low": None,
        "note": "仅日K量价结构，非方向结论",
    }


def _fetch_auction_evidence(symbol: str) -> dict:
    from src.core.auction_pool import fetch_auction_anomaly
    caliber = "9:25 竞价异动池 + TQ OpenAmo/OpenVolPre1/BCancel"
    try:
        rows = fetch_auction_anomaly("CN")
        recs = rows if isinstance(rows, list) else (rows.get("records") or [])
        hit = next((r for r in recs if r.get("symbol") == symbol or r.get("code") == symbol), None)
        if hit:
            return {
                "available": True,
                "caliber": caliber,
                "as_of": _now_cst_iso(),
                "degraded_reason": None,
                "gated": False,
                "verdict_tag": hit.get("verdict_tag"),
                "verdict_level": hit.get("verdict_level"),
                "verdict_action": hit.get("verdict_action"),
                "gap_pct": _safe_float(hit.get("gap_pct")),
                "open_amount": _safe_float(hit.get("open_amount")),
                "volume_ratio": _safe_float(hit.get("volume_ratio")),
                "withdraw_rate": _safe_float(hit.get("withdraw_rate")),
                "open_zt_buy": _safe_float(hit.get("open_zt_buy")),
            }
    except Exception as e:
        logger.warning("Auction evidence error for %s: %s", symbol, e)
    return {
        "available": False,
        "caliber": caliber,
        "as_of": None,
        "degraded_reason": "未命中今日竞价异动池",
        "gated": False,
        "verdict_tag": None,
        "verdict_level": None,
        "verdict_action": None,
        "gap_pct": None,
        "open_amount": None,
        "volume_ratio": None,
        "withdraw_rate": None,
        "open_zt_buy": None,
    }


def _fetch_demon_evidence(symbol: str) -> dict:
    from src.core.demon_factors import load_factor_pool
    caliber = "妖股六维因子库(涨停事件驱动)Top50 内/外"
    try:
        items = load_factor_pool(topn=200, min_total=0)
        hit = next((s for s in items if s.get("symbol") == symbol or s.get("code") == symbol), None)
        if hit:
            rank = hit.get("rank")
            return {
                "available": True,
                "caliber": caliber,
                "as_of": _now_cst_iso(),
                "degraded_reason": None,
                "gated": False,
                "total": _safe_float(hit.get("total_score") or hit.get("total")),
                "grade": hit.get("grade"),
                "max_streak": _safe_int(hit.get("max_streak")),
                "n_events": _safe_int(hit.get("n_events")),
                "n_sealed": _safe_int(hit.get("n_sealed")),
                "participation": _safe_float(hit.get("participation")),
                "dims": hit.get("dims") or {},
                "flags": hit.get("flags") or [],
                "in_top50": bool(rank and rank <= 50),
                "rank": rank,
            }
    except Exception as e:
        logger.warning("Demon evidence error for %s: %s", symbol, e)
    return {
        "available": False,
        "caliber": caliber,
        "as_of": None,
        "degraded_reason": "未进入妖股前列或普通标的",
        "gated": False,
        "total": None,
        "grade": None,
        "max_streak": None,
        "n_events": None,
        "n_sealed": None,
        "participation": None,
        "dims": {},
        "flags": [],
        "in_top50": False,
        "rank": None,
    }


def _fetch_theme_evidence(symbol: str) -> dict:
    from src.core.market_mainline import aggregate_mainline
    caliber = "主线 Top20 分组身位（涨停池驱动）"
    try:
        res = aggregate_mainline(limit_up_pool=None)
        groups = res.get("ranked_groups") or []
        for g in groups:
            constituents = g.get("constituents") or []
            if any(c.get("symbol") == symbol or c.get("code") == symbol for c in constituents):
                rank = g.get("rank", 99)
                return {
                    "available": True,
                    "caliber": caliber,
                    "as_of": _now_cst_iso(),
                    "degraded_reason": None,
                    "gated": False,
                    "group": g.get("name"),
                    "rank": rank,
                    "limit_up_count": _safe_int(g.get("limit_up_count")),
                    "leader": g.get("leader"),
                    "is_top5": bool(rank <= 5),
                }
    except Exception as e:
        logger.warning("Theme evidence error for %s: %s", symbol, e)
    return {
        "available": False,
        "caliber": caliber,
        "as_of": None,
        "degraded_reason": "未归属于前排活跃主线题材",
        "gated": False,
        "group": None,
        "rank": None,
        "limit_up_count": None,
        "leader": None,
        "is_top5": False,
    }


# ---------------------------------------------------------------------------
# 确定性判定引擎 (Intent / Defense Lines / Plans / Narrative)
# ---------------------------------------------------------------------------

def _eval_intent_rule(evidence: dict) -> tuple[str, str, Tone, list[str]]:
    """确定性意图规则真值表 R1~R9 (首条命中即停)。"""
    chips = evidence.get("chips") or {}
    res = evidence.get("resonance") or {}
    micro = evidence.get("microstructure") or {}
    wyckoff = evidence.get("wyckoff") or {}

    p_ratio = chips.get("profit_ratio")
    m_warn = micro.get("warning_level")
    m_tag = micro.get("intent_tag") or ""
    r_state = res.get("resonance_state") or ""
    r_warn = res.get("resonance_warning")
    gs_zone = res.get("gs_zone")
    l2_net = res.get("l2_net_wan")

    # R1: 获利盘>=85 + 盘口预警
    if p_ratio is not None and p_ratio >= 85 and m_warn == "warning":
        return "R1", "托单诱多 · 高位兑现压力", "risk", ["high_profit_ratio", "fake_bid"]

    # R2: 三指标全线走坏
    if r_warn == "warning":
        return "R2", "共振转空 · 资金与机构双撤", "risk", ["resonance_break"]

    # R3: 威科夫放量滞涨 + 获利盘>=70
    if wyckoff.get("effort_result") == "divergence" and p_ratio is not None and p_ratio >= 70:
        return "R3", "放量滞涨 · 上方有供应压制", "risk", ["effort_divergence"]

    # R4: 威科夫 Phase C 弹簧针 + 盘口非高危
    if wyckoff.get("stage") == "C" and m_warn != "warning":
        return "R4", "弹簧试探 · 洗盘待确认", "opportunity", ["spring_probe"]

    # R5: 三指标首次共振 (以 🔥 开头)
    if r_state.startswith("🔥"):
        return "R5", "三指标首次共振 · 趋势待启动", "opportunity", ["resonance_first"]

    # R6: 单峰密集 + 获利 20~60% + GS 在 G区
    if chips.get("pattern") == "单峰密集" and p_ratio is not None and 20 <= p_ratio <= 60 and gs_zone == "G":
        return "R6", "低位单峰 · 筹码锁定良好", "opportunity", ["single_peak"]

    # R7: G区但资金净流出
    if gs_zone == "G" and l2_net is not None and l2_net < 0:
        return "R7", "G 区资金背离 · 提防假突破", "caution", ["g_divergence"]

    # R8: 撤单异常标签
    if any(k in m_tag for k in ("托单诱多", "频繁试盘", "买单虚脱")):
        return "R8", "盘口撤单异常 · 支撑虚实未明", "caution", ["cancel_anomaly"]

    # R9: 兜底
    return "R9", "多空均衡 · 区间待方向", "neutral", []


def _compute_defense_lines(quote: dict, chips: dict, wyckoff: dict) -> list[dict]:
    close = quote.get("close")
    if close is None or close <= 0:
        return []

    candidates = [
        ("first", "第一防守位", quote.get("low"), "今日盘中探底低点"),
        ("core", "核心生命线", chips.get("cost_70_low"), "70%建仓筹码底沿"),
        ("probe", "弹簧针低点", wyckoff.get("spring_low"), "威科夫Phase C弹簧支撑"),
        ("cost", "底仓成本", chips.get("main_cost"), "主力底仓重仓均价"),
        ("cap", "强阻力压制", chips.get("cost_70_high"), "70%建仓筹码顶沿"),
    ]

    filtered = []
    for slot, label, price, basis in candidates:
        if price is None or price <= 0:
            continue
        p = float(price)
        # 偏离现价 > 25% 舍弃
        if abs(p - close) / close > 0.25:
            continue
        # 与已有价位 < 0.6% 则视为重复合并
        duplicate = False
        for ex in filtered:
            if abs(ex["price"] - p) / p < 0.006:
                duplicate = True
                ex["basis"] += f"，与{label}重合"
                break
        if not duplicate:
            side = "support" if p < close else "resistance"
            dist_pct = round((p - close) / close * 100.0, 2)
            filtered.append({
                "slot": slot,
                "label": label,
                "price": round(p, 2),
                "side": side,
                "distance_pct": dist_pct,
                "basis": basis,
                "broken": (side == "support" and close < p) or (side == "resistance" and close > p),
            })

    # 分组排序
    supports = sorted([x for x in filtered if x["side"] == "support"], key=lambda x: x["price"], reverse=True)
    resistances = sorted([x for x in filtered if x["side"] == "resistance"], key=lambda x: x["price"])
    return (supports + resistances)[:4]


def _build_action_plans(tone: Tone, defense_lines: list[dict], has_position: bool = True) -> list[dict]:
    first = next((x["price"] for x in defense_lines if x["slot"] in ("first", "core")), None)
    core = next((x["price"] for x in defense_lines if x["slot"] == "core"), None)
    cap = next((x["price"] for x in defense_lines if x["slot"] == "cap"), None)

    if first is None or core is None:
        return [
            {
                "audience": "holder",
                "action": "wait",
                "title": "持仓者",
                "text": "关键价位不完整，本次不给具体价位；参考分时均线与当日低点，待数据恢复后再做评估。",
                "triggers": [],
            },
            {
                "audience": "empty",
                "action": "wait",
                "title": "空仓者",
                "text": "数据缺失，不宜盲目进场参与。",
                "triggers": [],
            },
        ]

    f_str = f"{first:.2f}"
    c_str = f"{core:.2f}"
    cap_str = f"{cap:.2f}" if cap else "上方密集区"

    plan_matrix = {
        "risk": {
            "holder": ("reduce", f"防守为主：现价附近分批减至半仓以下；跌破 {f_str} 清仓离场，不做补仓摊薄。"),
            "empty": ("avoid", f"不参与：耐心等待重新收复 {c_str} 且 GS 信号转 G 再行跟踪，期间不猜底。"),
        },
        "caution": {
            "holder": ("hold", f"持有观察：不加仓；把防守位设在 {f_str} 下方 1%，破位先减半规避风险。"),
            "empty": ("wait", f"等待确认：耐心等待有效站上 {c_str} 且盘口撤单恢复正常后再评估。"),
        },
        "neutral": {
            "holder": ("hold", f"按兵不动：区间震荡内维持现有头寸，跌破核心线 {c_str} 减半防守。"),
            "empty": ("wait", f"观察为主：等待放量突破 {cap_str} 或回踩 {c_str} 确认企稳再谈介入。"),
        },
        "opportunity": {
            "holder": ("hold", f"持有待涨：顺势持有不追高；回踩 {c_str} 不破可适度加仓，跌破 {f_str} 减半。"),
            "empty": ("probe", f"分批试探：在 {c_str} 附近小仓试错，若跌破 {f_str} 坚决无条件离场。"),
        },
    }

    h_act, h_txt = plan_matrix[tone]["holder"]
    e_act, e_txt = plan_matrix[tone]["empty"]

    plans = [
        {"audience": "holder", "action": h_act, "title": "持仓者", "text": h_txt, "triggers": []},
        {"audience": "empty", "action": e_act, "title": "空仓者", "text": e_txt, "triggers": []},
    ]
    return plans


def _build_narrative(chips: dict, micro: dict, res: dict) -> str:
    parts = []
    if chips.get("available") and chips.get("main_cost") is not None:
        p_str = f"{chips.get('profit_ratio', 0):.1f}%" if chips.get('profit_ratio') is not None else "--"
        low = f"{chips.get('cost_70_low', 0):.2f}" if chips.get('cost_70_low') else "--"
        high = f"{chips.get('cost_70_high', 0):.2f}" if chips.get('cost_70_high') else "--"
        parts.append(f"底仓成本 {chips['main_cost']:.2f} 元，70% 筹码集中在 {low}–{high} 元，当前获利盘 {p_str}。")
    elif chips.get("degraded_reason"):
        parts.append(f"筹码模型：{chips['degraded_reason']}。")

    if micro.get("available") and micro.get("b_cancel_ratio") is not None:
        b = f"{micro['b_cancel_ratio'] * 100:.1f}%"
        s = f"{micro.get('s_cancel_ratio', 0) * 100:.1f}%"
        tag = micro.get("intent_tag") or "常态"
        parts.append(f"盘口买撤 {b}／卖撤 {s}，判定为【{tag}】。")

    if res.get("available") and res.get("resonance_state"):
        act = f"{res.get('activity', 0):.2f}" if res.get('activity') else "--"
        lvl = res.get("activity_level") or "--"
        net = f"{res.get('l2_net_wan', 0):.1f}" if res.get('l2_net_wan') is not None else "--"
        parts.append(f"三指标状态为【{res['resonance_state']}】，机构活跃度 {act}（{lvl}），L2 主力净额 {net} 万元。")

    return "".join(parts) or "当前标的多维量化证据正在汇聚中。"


# ---------------------------------------------------------------------------
# 顶层入口: build_copilot_brief
# ---------------------------------------------------------------------------

def build_copilot_brief(symbol: str, market: str = "CN", perms: list[str] | None = None, now: datetime | None = None) -> dict:
    """并发汇聚 7 块证据并产出确定性参谋 Brief。"""
    quote_res = _fetch_quote_evidence(symbol, market)

    evidence_getters = {
        "chips": lambda: _fetch_chips_evidence(symbol, market),
        "resonance": lambda: _fetch_resonance_evidence(symbol, market),
        "microstructure": lambda: _fetch_micro_evidence(symbol, market, quote_res),
        "wyckoff": lambda: _fetch_wyckoff_evidence(symbol, market),
        "auction": lambda: _fetch_auction_evidence(symbol),
        "demon": lambda: _fetch_demon_evidence(symbol),
        "theme": lambda: _fetch_theme_evidence(symbol),
    }

    evidence: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=7) as executor:
        futures = {executor.submit(fn): key for key, fn in evidence_getters.items()}
        for fut in as_completed(futures):
            key = futures[fut]
            try:
                evidence[key] = fut.result()
            except Exception as e:
                logger.warning("Evidence worker error for %s: %s", key, e)
                evidence[key] = {
                    "available": False,
                    "caliber": "",
                    "as_of": None,
                    "degraded_reason": f"模块异常: {e}",
                    "gated": False,
                }

    # 统计可用与缺失
    evidence_used = [k for k, v in evidence.items() if v.get("available")]
    evidence_missing = [k for k, v in evidence.items() if not v.get("available")]

    # 判定层
    rule_id, intent_label, intent_tone, risk_flags = _eval_intent_rule(evidence)

    # 置信度公式
    raw_conf = 0.35 + 0.09 * len(evidence_used) - 0.05 * len(evidence_missing)
    if "chips" in evidence_missing:
        raw_conf = min(raw_conf, 0.55)
    confidence = max(0.20, min(0.95, round(raw_conf, 2)))

    # 防守位与预案
    defense_lines = _compute_defense_lines(quote_res, evidence["chips"], evidence["wyckoff"])
    action_plans = _build_action_plans(intent_tone, defense_lines)
    narrative = _build_narrative(evidence["chips"], evidence["microstructure"], evidence["resonance"])

    caliber_notes = [v["caliber"] for v in evidence.values() if v.get("caliber")]

    verdict = {
        "intent_label": intent_label,
        "intent_tone": intent_tone,
        "confidence": confidence,
        "evidence_used": evidence_used,
        "evidence_missing": evidence_missing,
        "narrative": narrative,
        "defense_lines": defense_lines,
        "action_plans": action_plans,
        "risk_flags": risk_flags,
        "caliber_notes": caliber_notes,
        "disclaimer": DISCLAIMER,
        "rule_id": rule_id,
    }

    # 交易阶段识别
    phase_info = phase_meta()

    return {
        "symbol": symbol,
        "market": market,
        "name": None,
        "as_of": _now_cst_iso(),
        "trade_date": phase_info.get("data_date"),
        "session": {
            "phase": phase_info.get("trade_phase", "closed"),
            "phase_label": phase_info.get("phase_label", "已闭市"),
            "is_trading": bool(phase_info.get("is_live", False)),
        },
        "quote": quote_res,
        "evidence": evidence,
        "verdict": verdict,
        "cache": {"hit": False, "ttl_s": 20, "key": f"{symbol}:{market}"},
        "version": "copilot-brief/1",
    }
