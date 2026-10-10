"""决策合成(2026-09-08 方向2): 三信号 → 动手/看看/别碰 + 一行理由。

输入复用现有能力, 不重写算法:
- 趋势: gs_strategy.eval_gs + trend_label
- 活跃度: ai_activity.eval_activity(当日 + 砍末根算前日)
- 资金: dark_pool_flow.compute_pool_flow(symbol)["main_net"](明+暗; 未齐时为 None)
- 合成: resonance.evaluate_state(7 行状态表) → verdict 映射

verdict 映射: 向好→动手, 拐点/分歧→看看, 走坏→别碰;
缺数/表外(row=0)→看看(理由写清缺什么, 不编造方向)。

## 个性化(2026-10-10 P1-2): 合成层透明微调, 不动账本
`synthesize` 增可选 `user_context`(risk_profile/持仓/自选), 只做**透明微调**:
风险偏好改变『动手』阈值带宽(保守型收紧)、已持仓标的附『持仓成本/浮盈』参考行;
**verdict 语义表本身不改**, 且输出里显式标注哪部分因个性化调整(`personalized`/
`personalization_notes`/`risk_profile`/`position`), 可解释、不可静默。
缺 `user_context` → 全局口径照常(逐字段与旧版一致, 向后兼容零破坏)。
多用户 user_id 隔离是 AGENTS 硬约束, 上下文由 API 层按当前用户组装(禁全局混用)。

## 跨市场诚实降级(2026-10-10 P2-7): 非 CN 双维
非 CN(HK/US)无明/暗盘资金源(compute_pool_flow 为 A 股口径) → `synthesize_two_dimension`
用**趋势 × 活跃度**双维出 verdict, 显式标 `basis="two-dimension"` + `fund_note="资金维无数据(非CN)"`
—— **禁编造资金、禁静默 None**。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_VERDICT_BY_PHASE = {
    "向好": "动手",
    "拐点": "看看",
    "分歧": "看看",
    "走坏": "别碰",
}

# 非 CN 双维判定的 basis 标记(诚实降级: 资金维无源)
BASIS_TWO_DIMENSION = "two-dimension"
FUND_MISSING_NOTE_NON_CN = "资金维无数据(非CN)"


def synthesize(
    trend: str | None,
    activity: float | None,
    activity_prev: float | None,
    fund_net: float | None,
    fund_net_prev: float | None = None,
    user_context: dict | None = None,
    *,
    regime: str | None = None,
    bdqk: dict | None = None,
) -> dict:
    """纯函数: 三信号 → {verdict, reason, parts}。无 IO, 可单测。

    user_context(可选, 2026-10-10 P1-2): {risk_profile, holds, cost_price, quantity,
    in_watchlist, last_close}。缺省 None → 全局口径, 输出逐字段与旧版一致。

    regime(可选, 2026-10-10 A 决策提胜率): 当前情绪周期态(冰点/修复/发酵/高潮/退潮,
    见 `src.core.market_regime`); 对『动手』门槛做**显式可解释**的条件化。缺省 None
    → 视作 unknown(标准口径, 向后兼容零破坏)。

    bdqk(可选, 2026-10-10 B 决策提胜率): 内外盘七口诀判定结果(`_judge_mnemonic` 返回体
    或 None); 作为叠加确认维度。缺省 None → 视为『难归集』(不叠加)。

    处理顺序(透明、逐级留痕): 基线 → 情绪周期条件化 → 七口诀叠加 → 决策先锋口径 → 个性化。
    """
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
        note = st.get("note")
        if note:
            reason = f"信号不全({note}), 先别动手"  # 数据缺失: 明说缺哪个
        else:
            # 三信号齐全但组合不在 7 行状态表(row 0 无共振): 不冒充"信号不全"
            reason = f"未共振({st.get('state') or '组合不在状态表'}), 先观察"
        out = {
            "verdict": "看看",
            "reason": reason,
            "phase": phase,
            "row": st.get("row", 0),
            "parts": _parts_text(trend, activity, fund_net),
        }
    else:
        verdict = _VERDICT_BY_PHASE.get(phase, "看看")
        out = {
            "verdict": verdict,
            "reason": _reason_line(verdict, trend, activity, activity_prev, fund_net, st),
            "phase": phase,
            "row": st.get("row", 0),
            "parts": _parts_text(trend, activity, fund_net),
        }
    out = _apply_regime(out, regime)
    out = _apply_bdqk(out, bdqk)
    return _personalize(_attach_pioneer_terms(out), user_context)


# 四态 → 决策先锋口径「资金博弈」表述(口径对应, 只做中文命名对齐, 不改判定)
_PIONEER_FUND_BY_PHASE = {
    "向好": "多方占优",
    "拐点": "多空转换",
    "分歧": "多空分歧",
    "走坏": "空方占优",
}


def _attach_pioneer_terms(out: dict) -> dict:
    """附决策先锋口径的中文表述(2026-10-10 口径对齐)。

    数智决策为**仿制同花顺「决策先锋」**的三指标体系; 本字段把内部判定映射到
    决策先锋习惯的表述, 便于前端/报告对照展示(纯增量, **不改 verdict 语义**):
      - 情绪周期: 冰点/修复/发酵/高潮/退潮(与决策先锋"情绪周期/市场情绪"口径对应);
      - 资金博弈: 多方占优/多空转换/多空分歧/空方占优(对应四态 向好/拐点/分歧/走坏);
      - 主力动向: 内外盘七口诀结论(真金进攻/主力撤退/诱多出货...); 无 → '未归集'。
    """
    out["pioneer_terms"] = {
        "情绪周期": out.get("regime_label") or "数据不足",
        "资金博弈": _PIONEER_FUND_BY_PHASE.get(out.get("phase") or "", "无数据"),
        "主力动向": (out.get("bdqk") or {}).get("name") or "未归集",
    }
    return out


def _apply_regime(out: dict, regime: str | None) -> dict:
    """情绪周期条件化(2026-10-10 A)。透明留痕, 缺 data 显式, 缺参数零破坏。

    输出附加(始终存在, 便于前端/审计显式看到条件化与否):
      - `regime` / `regime_label`: 规范 key + 中文名;
      - `regime_policy`: tighten / normal / loosen;
      - `regime_note`: 条件化说明(未调整时也给当前态口径说明);
      - `regime_adjusted`: 本次是否因 regime 改了 verdict。
    """
    from src.core.market_regime import condition_verdict, regime_policy

    pol = regime_policy(regime)
    prev = out.get("verdict") or ""
    verdict, adjusted, note = condition_verdict(prev, out.get("row"), regime)
    out["verdict"] = verdict
    out["regime"] = pol["regime"]
    out["regime_label"] = pol["label"]
    out["regime_policy"] = pol["policy"]
    out["regime_adjusted"] = adjusted
    if adjusted and note:
        out["regime_note"] = note
        out["reason"] = f"{note}；{out.get('reason', '')}"
    else:
        out["regime_note"] = pol["note"]
    return out


def _apply_bdqk(out: dict, bdqk: dict | None) -> dict:
    """内外盘七口诀叠加确认(2026-10-10 B)。缺数据显式, 差异可见。

    输出附加:
      - `bdqk`: {name, direction, effect, note}(无则 None);
      - `bdqk_effect`: confirm / suppress / neutral / none / unavailable;
      - `bdqk_note` / `bdqk_adjusted` / `bdqk_signal_kind`(入账用, 无则 None)。
    """
    from src.core.mnemonic_overlay import eval_bdqk_effect

    eff = eval_bdqk_effect(bdqk)
    out["bdqk"] = {
        "name": eff["name"],
        "direction": eff["direction"],
        "effect": eff["effect"],
        "note": eff["note"],
    }
    out["bdqk_effect"] = eff["effect"]
    out["bdqk_note"] = eff["note"]
    out["bdqk_adjusted"] = False
    out["bdqk_signal_kind"] = None

    if eff["effect"] == "confirm" and out.get("verdict") == "动手":
        # 同向确认 → 动手加成(verdict 不变, 但标记已叠加确认)
        out["bdqk_note"] = f"{eff['note']} → 与共振同向, 动手叠加加成"
        out["bdqk_signal_kind"] = eff["signal_kind"]
        out["reason"] = f"{out.get('reason', '')}｜七口诀『{eff['name']}』叠加确认(动手加成)"
    elif eff["effect"] == "suppress" and out.get("verdict") == "动手":
        # 出货/撤退 → 直接压制『动手』
        out["verdict"] = "看看"
        out["bdqk_adjusted"] = True
        out["bdqk_signal_kind"] = eff["signal_kind"]
        out["reason"] = (
            f"七口诀『{eff['name']}』压制『动手』 → 收敛为『看看』；{out.get('reason', '')}"
        )
    elif eff["effect"] in ("neutral", "none", "unavailable"):
        out["bdqk_note"] = eff["note"] + "(不叠加)"
    elif out.get("verdict") != "动手":
        # confirm 但当前不是『动手』: 如实说"未生效", 不硬改 verdict
        out["bdqk_note"] = f"{eff['note']} (当前非『动手』, 未生效)"
    return out


def synthesize_two_dimension(
    trend: str | None,
    activity: float | None,
    activity_prev: float | None,
    user_context: dict | None = None,
) -> dict:
    """非 CN(HK/US)资金维无源时的**双维(趋势 × 活跃度)**判定(2026-10-10 P2-7)。

    诚实降级: 资金维**无数据**(非 CN 无明/暗盘源), 显式标 `basis="two-dimension"` +
    `fund_note="资金维无数据(非CN)"` —— **不编造资金、不静默 None**。

    verdict 口径(两维, 活跃度阈值复用阈值配置层 strong_line()=3.00 可配):
      - 趋势 G(信号/区间) + 活跃度站上强势线 → 动手(向好)
      - 趋势 S(信号/区间) + 活跃度跌破强势线 → 别碰(走坏)
      - 其余(G 但活跃度弱 / S 但活跃度强) → 看看(分歧)
      - 缺数(趋势/活跃度任一缺) → 看看 + 理由写清缺哪个
    """
    from src.core.thresholds import strong_line

    missing = []
    if not trend or trend == "无数据":
        missing.append("趋势")
    if activity is None:
        missing.append("活跃度")

    if missing:
        verdict, phase = "看看", "无"
        reason = (
            f"信号不全(缺失: {'/'.join(missing)})；{FUND_MISSING_NOTE_NON_CN}, "
            f"仅趋势×活跃度双维, 先别动手"
        )
    else:
        act_ok = activity is not None and activity >= strong_line()
        if trend in ("G信号", "G区间") and act_ok:
            verdict, phase = "动手", "向好"
        elif trend in ("S信号", "S区间") and not act_ok:
            verdict, phase = "别碰", "走坏"
        else:
            verdict, phase = "看看", "分歧"
        reason = _two_dim_reason(verdict, trend, activity, activity_prev)

    out = {
        "verdict": verdict,
        "reason": reason,
        "phase": phase,
        # 双维无 7 行状态表行号 → 显式 0(不冒充三信号状态表)
        "row": 0,
        "basis": BASIS_TWO_DIMENSION,
        "fund_note": FUND_MISSING_NOTE_NON_CN,
        "parts": {"trend": trend or "无数据", "activity": activity, "fund_net": None},
    }
    return _personalize(out, user_context)


def _personalize(out: dict, user_context: dict | None) -> dict:
    """个性化透明微调(2026-10-10 P1-2)。无上下文 → 原样返回(向后兼容零破坏)。

    只改 verdict 判定的**阈值带宽**, 不改 verdict 语义表本身; 附持仓参考行;
    所有因个性化产生的变化都写入 `personalization_notes`(可解释, 幻觉敏感红线)。
    """
    if not user_context:
        return out
    ctx = user_context
    rp = (ctx.get("risk_profile") or "").strip().lower() or None
    basis = out.get("basis")
    notes: list[str] = []
    prev_verdict = out.get("verdict")

    # ① 风险偏好 → 『动手』阈值带宽(仅收紧/放开 动手 档, 不动语义表)
    if rp == "conservative" and prev_verdict == "动手":
        if basis == BASIS_TWO_DIMENSION:
            # 非 CN 双维: 本就无资金维确认, 保守型把『动手』收敛为『看看』
            notes.append("个性化(保守型)：非 CN 无资金维确认,『动手』收敛为『看看』")
            out["verdict"] = "看看"
        elif out.get("row") not in (1, 2):
            # CN 三信号: 仅三指标首次/再次共振(状态表行1/2)才给『动手』
            notes.append(
                f"个性化(保守型)：『动手』收紧至三指标首次/再次共振(状态表行1/2), "
                f"当前行{out.get('row')}({out.get('phase')})收敛为『看看』"
            )
            out["verdict"] = "看看"
    elif rp == "aggressive" and prev_verdict == "看看" and out.get("phase") == "拐点":
        notes.append("个性化(激进型)：拐点(再次共振)提前放开为『动手』")
        out["verdict"] = "动手"

    # ② 已持仓 → 附『持仓成本/浮盈』参考行(不改 verdict 语义表本身)
    position = None
    if ctx.get("holds"):
        cost = ctx.get("cost_price")
        last = ctx.get("last_close")
        qty = ctx.get("quantity")
        pnl_pct = None
        if isinstance(cost, (int, float)) and cost > 0 and isinstance(last, (int, float)):
            pnl_pct = round((last - cost) / cost * 100, 2)
        position = {
            "cost_price": cost if isinstance(cost, (int, float)) else None,
            "quantity": qty if isinstance(qty, int) else None,
            "last_close": last if isinstance(last, (int, float)) else None,
            "pnl_pct": pnl_pct,
            "note": _position_note(cost, pnl_pct),
        }

    out["personalized"] = bool(notes) or position is not None
    out["personalization_notes"] = notes
    out["personalization_note"] = "；".join(notes) if notes else None
    out["risk_profile"] = rp
    out["in_watchlist"] = bool(ctx.get("in_watchlist"))
    out["position"] = position
    return out


def _position_note(cost: Any, pnl_pct: Optional[float]) -> str:
    """持仓参考行文案: 持仓成本 / 浮盈(成本缺 → 显式'无数据', 不编造)。"""
    if not isinstance(cost, (int, float)) or cost <= 0:
        return "已持仓：持仓成本无数据"
    if pnl_pct is None:
        return f"已持仓：持仓成本 {cost:.2f}(现价无数据)"
    sign = "+" if pnl_pct >= 0 else ""
    return f"已持仓：持仓成本 {cost:.2f} / 浮盈 {sign}{pnl_pct:.2f}%"


def personalize(out: dict, user_context: dict | None) -> dict:
    """公开入口: 对(可能是预落库的)全局基底施加用户个性化透明微调(2026-10-10 冗余设计)。

    即 `_personalize` 的公开别名 —— 供 `decision_cache.apply_user_overlay` 在不 import
    私有名的前提下复用同一套个性化口径(单一事实来源, 不复制逻辑)。**原地改 out**,
    调用方需先深拷贝(缓存层负责)。
    """
    return _personalize(out, user_context)


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


def _two_dim_reason(verdict, trend, activity, activity_prev) -> str:
    """双维理由行: 趋势 + 活跃度, 并显式标注资金维无数据(非CN)。"""
    bits = [f"趋势{trend}"]
    if activity is None:
        bits.append("活跃度无数据")
    else:
        s = f"活跃度{activity:.2f}"
        if activity_prev is not None and activity_prev > 0 and activity >= 2 * activity_prev:
            s += "(较前日翻倍)"
        bits.append(s)
    return f"{verdict}: {'、'.join(bits)}；{FUND_MISSING_NOTE_NON_CN}, 仅趋势×活跃度双维"


def _last_bar_close(bars: list[dict]) -> Optional[float]:
    """末根收盘价(None 安全, 不编造)。供预落库基底携带 last_close 做读取时浮盈。"""
    try:
        last = bars[-1]
        close = last.get("close") if isinstance(last, dict) else getattr(last, "close", None)
        return float(close) if close is not None else None
    except Exception:  # noqa: BLE001
        return None


def _attach_last_close(user_context: dict | None, bars: list[dict]) -> dict | None:
    """给上下文补现价(末根收盘), 供浮盈计算(缺则置 None, 不编造)。"""
    if not user_context:
        return user_context
    ctx = dict(user_context)
    try:
        last = bars[-1]
        close = last.get("close") if isinstance(last, dict) else getattr(last, "close", None)
        ctx["last_close"] = float(close) if close is not None else None
    except Exception:  # noqa: BLE001
        ctx["last_close"] = None
    return ctx


def _mark_non_cn(out: dict, market: str) -> dict:
    """非 CN 的降级标注(仅加标记, 不改 verdict 语义)。"""
    if (market or "CN").upper() != "CN":
        out["basis"] = BASIS_TWO_DIMENSION
        out["fund_note"] = FUND_MISSING_NOTE_NON_CN
    return out


def decide(symbol: str, market: str = "CN", days: int = 120, user_context: dict | None = None) -> dict:
    """IO 入口: 拉 K 线 → 三信号 → synthesize。失败一律看看 + 理由, 不抛。

    market: CN 走三信号(趋势×活跃度×资金); HK/US 资金维无源 → 双维诚实降级
    (见 `synthesize_two_dimension`)。
    user_context(可选): 当前用户上下文, 由 API 层按 user_id 隔离组装。

    2026-10-10 决策提胜率: CN 路径额外读取**当前情绪周期态**(`market_regime`)做
    『动手』门槛条件化, 并拉**内外盘七口诀**(`mnemonic_overlay`)做叠加确认; 叠加
    确认/压制结果入账 `decision_log`(signal_kind=bdqk_confirm / bdqk_suppress)。
    非 CN / 取数失败一律显式(regime=unknown, bdqk 难归集), 不硬凑、不编造。
    """
    mkt = (market or "CN").upper()
    try:
        from src.collectors.kline_collector import KlineCollector
        from src.models.market import MarketCode
        from src.core.gs_strategy import eval_gs, trend_label
        from src.core.ai_activity import eval_activity

        mc = MarketCode(mkt)
        klines = KlineCollector(mc).get_klines(symbol, days=days)
        bars = [
            {"date": k.date, "open": k.open, "close": k.close,
             "high": k.high, "low": k.low, "volume": k.volume}
            for k in (klines or [])
        ]
        if not bars:
            out = {"symbol": symbol, "verdict": "看看",
                   "reason": "看看: 无 K 线数据, 先别动手", "phase": "无", "row": 0,
                   "parts": _parts_text(None, None, None), "last_close": None}
            return _mark_non_cn(out, mkt)
        trend = trend_label(eval_gs(bars))
        act = eval_activity(bars)
        activity = act.get("activity") if isinstance(act, dict) else None
        act_prev = eval_activity(bars[:-1])
        activity_prev = act_prev.get("activity") if isinstance(act_prev, dict) else None
        ctx = _attach_last_close(user_context, bars)

        if mc != MarketCode.CN:
            # 非 CN: 资金维无源 → 双维诚实降级(不调 compute_pool_flow, 不编造资金)
            out = synthesize_two_dimension(trend, activity, activity_prev, ctx)
        else:
            try:
                from src.core.dark_pool_flow import compute_pool_flow

                # compute_pool_flow 返回 dict(main_net 可能为 None: 明/暗盘未齐时不硬算)
                pool = compute_pool_flow(symbol)
                fund_net = (pool or {}).get("main_net")
            except Exception as e:  # noqa: BLE001
                logger.debug("decision fund %s failed: %s", symbol, e)
                fund_net = None
            regime_key = _resolve_regime_key()
            mnemonic = _resolve_mnemonic_safe(symbol)
            out = synthesize(
                trend, activity, activity_prev, fund_net, None, ctx,
                regime=regime_key, bdqk=mnemonic,
            )
        out["symbol"] = symbol
        # 末根收盘(供预落库后**读取时**算持仓浮盈 —— 缓存层把全局基底与 last_close 一起存)。
        out["last_close"] = _last_bar_close(bars)
        if mc == MarketCode.CN:
            _log_decision_overlay(symbol, out)
        return out
    except Exception as e:  # noqa: BLE001 - 决策口永不 500
        logger.warning("decision %s failed: %s", symbol, e)
        out = {"symbol": symbol, "verdict": "看看",
               "reason": f"看看: 计算失败({type(e).__name__}), 先别动手",
               "phase": "无", "row": 0, "parts": _parts_text(None, None, None),
               "last_close": None}
        return _mark_non_cn(out, mkt)


def _resolve_regime_key() -> str:
    """读当前情绪周期态 → 规范 key。读失败/缺数据 → 'unknown'(显式, 不猜)。"""
    try:
        from src.core.market_regime import current_regime

        return current_regime().get("regime") or "unknown"
    except Exception as e:  # noqa: BLE001
        logger.debug("regime 解析失败(按 unknown 放行): %r", e)
        return "unknown"


def _resolve_mnemonic_safe(symbol: str) -> dict | None:
    """拉七口诀(难归集 → None)。绝不影响主流程。"""
    try:
        from src.core.mnemonic_overlay import resolve_mnemonic

        return resolve_mnemonic(symbol)
    except Exception as e:  # noqa: BLE001
        logger.debug("七口诀解析失败(难归集): %r", e)
        return None


def _log_decision_overlay(symbol: str, out: dict) -> int:
    """把七口诀叠加(确认/压制)结果入账决策账本(旁路, 永不抛)。

    - signal_kind = out['bdqk_signal_kind'](bdqk_confirm / bdqk_suppress), 只有
      可行动的两类非空才写; 无价/无 kinds → 不写(不硬填 0)。
    - 记录 regime(供账本按情绪周期分桶统计), 幂等(同 kind+symbol+trade_date UPSERT)。
    """
    kind = out.get("bdqk_signal_kind")
    if not kind:
        return 0
    price = out.get("last_close")
    if not isinstance(price, (int, float)):
        return 0  # 取不到价 → 不入账(不硬填 0)
    try:
        from src.core.decision_log import record_many_safe

        rows = [{
            "signal_kind": kind,
            "symbol": symbol,
            "price": float(price),
            "regime": out.get("regime") or "unknown",
            "context": {
                "regime": out.get("regime"),
                "regime_policy": out.get("regime_policy"),
                "verdict": out.get("verdict"),
                "phase": out.get("phase"),
                "row": out.get("row"),
                "bdqk_name": (out.get("bdqk") or {}).get("name"),
                "bdqk_effect": out.get("bdqk_effect"),
            },
        }]
        return record_many_safe(rows, source="decision_synth")
    except Exception as e:  # noqa: BLE001 —— 旁路失败只 warn
        logger.debug("决策叠加入账失败(不影响决策): %r", e)
        return 0
