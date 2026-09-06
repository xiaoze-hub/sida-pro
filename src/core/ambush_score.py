"""埋伏四维评分合成(批次C C2, 2026-09-06 28号)。

核心公式: 埋伏价值 = 事件确定性 × 传导位置 × 情绪容许度 × 信号不冲突
(每维 0-10, 等权平均后归一到 0-10; 情绪高潮硬否决 → action=禁推)。

输入: catalyst_screener.build_ambush_list 的埋伏榜条目
[{symbol, catalyst_date, catalyst_type, gap, reason, catalyst, codes}]。
信号维(暗盘不流出/活跃度非快速回落)缺数据时按三维归一并 flags 标注
(诚实口径: 信号维缺失不扣分也不送分)。

风险日历: 未来 15 日内该标的有解禁(来自 catalyst_calendar) → 扣 3 分 + 标注。
证伪条件: 规则生成(推演, 与实测数据显式分离), 每候选必带。
先锋组: 候选代码命中妖股池 TopN(B 批次) → 传导维 +2(封顶 10)。
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")

_GAP_SCORE = {"高": 8, "中": 5, "低": 3}
_TYPE_BONUS = {"解禁": 0}  # 解禁类纯利空, 不因临近加分


def _days_until(date_str: str, today: datetime) -> int | None:
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
        return (d - today.date()).days
    except (TypeError, ValueError):
        return None


def score_event(catalyst_type: str, gap: str, catalyst_date: str, today: datetime) -> int:
    """事件维: 预期差等级为主, 临近度调制; 解禁类不因临近加分。"""
    base = _GAP_SCORE.get(gap, 4)
    days = _days_until(catalyst_date, today)
    if days is not None and catalyst_type != "解禁":
        if days <= 3:
            base += 2
        elif days <= 7:
            base += 1
        elif days > 15:
            base -= 1
    return max(0, min(10, base))


def score_transmission(codes: list[dict], demon_symbols: set[str] | None) -> int:
    """传导维: 受益落代码的高置信覆盖面 + 妖股先锋组加成。"""
    if not codes:
        return 2
    hi = sum(1 for c in codes if c.get("confidence") == "高")
    score = 4 + min(4, hi)  # 4-8
    if demon_symbols and any(c.get("symbol") in demon_symbols for c in codes):
        score = min(10, score + 2)
    return max(0, min(10, score))


def demon_boost(
    codes: list[dict], demon_factors: dict[str, dict], mood: dict | None
) -> tuple[float, list[str], bool]:
    """妖股因子加成(批次C×B 集成, 2026-09-06): 落代码命中先锋组 → 按等级加分。

    返回 (boost, flags, demon_hit)。退潮/高潮期(demon_veto)先锋组反向禁推。
    """
    flags: list[str] = []
    if not codes or not demon_factors:
        return 0.0, flags, False
    hits = [demon_factors[c["symbol"]] for c in codes if c.get("symbol") in demon_factors]
    if not hits:
        return 0.0, flags, False
    best = max(hits, key=lambda f: f.get("total") or 0)
    grade = best.get("grade") or ""
    boost = float({"极妖": 3, "妖": 2, "活跃": 1}.get(grade, 1))
    flags.append(
        f"妖股先锋组: {best.get('name') or best.get('symbol')} {grade}"
        f"(股性分 {best.get('total')}, 近一年封板 {best.get('n_sealed')} 次, "
        f"最高 {best.get('max_streak')} 连板) +{boost}"
    )
    if mood and mood.get("demon_veto"):
        flags.append("⛔ 情绪退潮/高潮档位, 先锋组禁推(妖股退潮期跌最狠)")
        return -boost, flags, True
    return boost, flags, True


def score_mood(mood: dict) -> int:
    """情绪维: 容许度直通。缺数据 → 中性 5(不奖不罚) + 由调用方标注。"""
    if not mood.get("available"):
        return 5
    return int(mood.get("allowance") or 0)


def score_signal(symbol: str, l2: dict | None) -> tuple[int | None, list[str]]:
    """信号维: 暗盘/主力净额不流出 + 撤单不异常。缺数据 → (None, flags)。

    MVP 口径: fetch_tq_l2 的 zjl_hb(主力净额, 逐笔 L2 成品) < 0 → 信号冲突扣分;
    > 0 加分; 缺数据 → None(三维归一)。
    """
    if not l2 or l2.get("zjl_hb") is None:
        return None, [f"{symbol} 信号维缺数据(L2 主力净额不可用), 按三维归一"]
    net = float(l2["zjl_hb"])
    if net < 0:
        return 2, []
    if net > 0:
        return 8, []
    return 5, []


def risk_deduction(symbol: str, calendar: list[dict], today: datetime) -> tuple[float, list[str]]:
    """风险日历: 15 日内解禁 → 扣 3 + 标注。"""
    ded, flags = 0.0, []
    for c in calendar or []:
        if c.get("type") == "解禁" and (c.get("symbol") or "") == symbol:
            days = _days_until(c.get("date"), today)
            if days is not None and 0 <= days <= 15:
                ded -= 3
                flags.append(f"{days} 日后解禁({c.get('date')}), 埋伏扣分")
    return ded, flags


def build_invalidations(catalyst_type: str, catalyst_date: str, reason: str) -> list[str]:
    """证伪条件(规则推演, 显式与实测数据分离): 什么情况说明逻辑错了。"""
    inv = [
        f"催化日({catalyst_date or '待定'})前无相关公告/事件兑现 → 逻辑证伪, 放弃埋伏",
        "买入后跌破埋伏日 20 日最低价 → 技术证伪, 止损离场",
    ]
    if catalyst_type == "解禁":
        inv.append("解禁规模超流通盘 10% 且股东有减持预告 → 利空证伪'利空出尽'逻辑")
    if reason and ("涨价" in str(reason) or "提价" in str(reason)):
        inv.append("产品提价函被官方辟谣/取消 → 涨价逻辑证伪")
    return inv


def enrich_ambush_list(
    ambush_list: list[dict],
    mood: dict | None = None,
    calendar: list[dict] | None = None,
    demon_symbols: set[str] | None = None,
    signal_lookup=None,
    today: datetime | None = None,
    demon_factors: dict[str, dict] | None = None,
) -> list[dict]:
    """埋伏榜 → 四维评分增强榜(主入口, 永不抛异常)。

    signal_lookup(symbol) -> dict|None: 注入 L2 信号(默认 fetch_tq_l2), 测试可替。
    demon_factors: {symbol: factor} 先锋组因子映射(批次C×B)——命中按等级加成,
      退潮/高潮期反向禁推。demon_symbols 为旧参数(仅 +2 兼容)。
    高潮期(veto) → 全部 action=禁推, total 保留但标 veto。
    """
    today = today or datetime.now(_CST)
    mood = mood or {}
    demon_factors = demon_factors or {}
    demon_set = set(demon_factors.keys()) if demon_factors else (demon_symbols or set())
    out = []
    for item in ambush_list or []:
        if not isinstance(item, dict) or not item.get("symbol"):
            continue
        try:
            flags: list[str] = []
            s_event = score_event(item.get("catalyst_type", ""), item.get("gap", ""), item.get("catalyst_date", ""), today)
            s_trans = score_transmission(item.get("codes") or [], demon_set)
            s_mood = score_mood(mood)
            if not mood.get("available"):
                flags.append("情绪维缺数据(market_phase 未 sync), 按中性 5 计")
            s_signal, sig_flags = score_signal(item["symbol"], signal_lookup(item["symbol"]) if signal_lookup else None)
            flags.extend(sig_flags)

            if s_signal is None:
                total = (s_event + s_trans + s_mood) / 3.0
            else:
                total = (s_event + s_trans + s_mood + s_signal) / 4.0
            boost, bflags, demon_hit = demon_boost(item.get("codes") or [], demon_factors, mood)
            total += boost
            flags.extend(bflags)
            ded, rflags = risk_deduction(item["symbol"], calendar, today)
            flags.extend(rflags)
            total = max(0.0, min(10.0, total + ded))

            veto = bool(mood.get("veto")) or (demon_hit and bool(mood.get("demon_veto")))
            action = (
                "禁推(情绪高潮)" if mood.get("veto")
                else "禁推(先锋组退潮期)" if demon_hit and mood.get("demon_veto")
                else "观察" if total >= 5
                else "仅跟踪"
            )
            out.append({
                **item,
                "ambush_total": round(total, 1),
                "dims": {"event": s_event, "transmission": s_trans, "mood": s_mood, "signal": s_signal},
                "demon_hit": demon_hit,
                "flags": flags,
                "invalidations": build_invalidations(item.get("catalyst_type", ""), item.get("catalyst_date", ""), item.get("reason", "")),
                "action": action,
            })
        except Exception as e:  # noqa: BLE001
            logger.warning("ambush score %s 失败: %s", item.get("symbol"), e)
    out.sort(key=lambda x: -x.get("ambush_total", 0))
    return out
