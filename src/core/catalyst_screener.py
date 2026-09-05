"""埋伏漏斗(2026-09-05, v0.5.6 CKPT3)。

日历/事件候选 → 纯规则打分(零 LLM)取 TopN → LLM 预期差 → 受益落代码 → 埋伏榜。
规则层可疑的一律丢进榜外, LLM 只看便宜且值得的标的(成本封顶)。

输出 [{symbol, catalyst_date, catalyst_type, gap, reason, codes[]}],
按 gap(高>中>低) + 催化临近度排序。
"""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)

TOPN_DEFAULT = 12
_MAX_RULE_CANDIDATES = 100
_GAP_RANK = {"高": 0, "中": 1, "低": 2}

# 规则: 已大涨/涨停的不埋伏(追高), 只做未反应
_CHG_CEIL = 8.0
_LIMITUP = 9.5


def _rule_candidates(calendar: list[dict]) -> list[dict]:
    """日历 → 去重候选(有 symbol 才留)。"""
    seen: dict[str, dict] = {}
    for c in calendar or []:
        sym = (c.get("symbol") or "").strip()
        if not sym or sym in seen:
            continue
        seen[sym] = {"symbol": sym, "date": c.get("date", ""), "type": c.get("type", ""), "title": c.get("title", "")}
    return list(seen.values())


def _snapshots(symbols: list[str]) -> dict[str, dict]:
    """批量快照(失败 → 空 dict, 上游降级为空榜)。"""
    try:
        from src.core.marketdata_client import md_quote_rows

        rows = md_quote_rows(symbols[:_MAX_RULE_CANDIDATES], "CN") or []
        return {r["symbol"]: r for r in rows if r.get("symbol")}
    except Exception as e:
        logger.debug(f"批量快照失败: {e}")
        return {}


def _rule_score(cand: dict, snap: dict, today: str) -> float | None:
    """纯规则打分(分越高越值得 LLM 看)。涨停/大涨 → None(丢掉)。"""
    chg = snap.get("change_pct")
    try:
        chg_f = float(chg) if chg is not None else 0.0
    except (TypeError, ValueError):
        chg_f = 0.0
    if chg_f >= _LIMITUP or chg_f > _CHG_CEIL:
        return None
    try:
        days = (date.fromisoformat(cand["date"]) - date.fromisoformat(today)).days if cand.get("date") else 30
    except ValueError:
        days = 30
    days = max(0, min(days, 30))
    vr = snap.get("volume_ratio") or 0
    try:
        vr_f = float(vr)
    except (TypeError, ValueError):
        vr_f = 0.0
    # 临近 +30~-days, 未涨 +(8-chg), 放量 +(vr capped 3)
    return (30 - days) + max(0.0, _CHG_CEIL - chg_f) + min(vr_f, 3.0)


def build_ambush_list(
    calendar: list[dict],
    today: str | None = None,
    topn: int = TOPN_DEFAULT,
    run_llm=None,
) -> list[dict]:
    """埋伏榜主入口。run_llm 为 LLM 函数注入(默认调真实引擎, 测试可替)。

    永远返回列表, 不抛异常。LLM/快照任一失败 → 空榜或部分榜。
    """
    from datetime import date as _d

    today = today or _d.today().isoformat()
    cands = _rule_candidates(calendar)
    if not cands:
        return []
    snaps = _snapshots([c["symbol"] for c in cands])
    if not snaps:
        return []
    scored = []
    for c in cands:
        s = _rule_score(c, snaps.get(c["symbol"], {}), today)
        if s is not None:
            scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    short = scored[:topn]
    if run_llm is None:
        from src.core.event_catalyst_engine import analyze_event_catalyst

        def _default_llm(sym, extra):  # noqa: ANN001, ANN202
            snap = snaps.get(sym, {})
            market = {k: snap.get(k) for k in ("current_price", "change_pct", "volume_ratio", "turnover_rate", "quote_date")}
            return analyze_event_catalyst(sym, None, market, extra)

        run_llm = _default_llm

    from src.core.beneficiary_resolver import resolve_beneficiaries

    out = []
    for _, c in short:
        try:
            extra = [f"{c['date']}{c['type']}: {c['title']}"]
            r = run_llm(c["symbol"], extra)
        except Exception as e:
            logger.debug(f"埋伏 LLM 失败 {c['symbol']}: {e}")
            continue
        if not r:
            continue
        codes = [x for x in resolve_beneficiaries(r.get("beneficiary_pool", [])) if x["confidence"] != "低"]
        out.append({
            "symbol": c["symbol"],
            "catalyst_date": c["date"],
            "catalyst_type": c["type"],
            "gap": (r.get("expectation_gap") or {}).get("level", ""),
            "reason": r.get("reason", ""),
            "catalyst": r.get("catalyst", ""),
            "codes": codes,
        })
    out.sort(key=lambda x: (_GAP_RANK.get(x["gap"], 9), x["catalyst_date"]))
    return out
