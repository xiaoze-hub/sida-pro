# -*- coding: utf-8 -*-
"""聚宝盆选股(数智决策 规格 §4.2 官方选股流程)。

官方选股流程(选股共振): **暗盘资金流入 + AI 机构活跃度 > 6(>12 更佳) + GS 在 G 区且出现
G 信号 + 问财关键词**。四条件按 **AND** 联合筛选; 本模块产出**逐条件通过/未过明细**(证据,
非建议), 任一必需条件缺数据 → 该股显式降级、**不出池**(不编造、不猜测)。

## 条件口径
| key          | 条件                     | 判定                                                        | 缺数据   |
|--------------|--------------------------|-------------------------------------------------------------|----------|
| dark_inflow  | 暗盘资金流入             | 暗盘净额 > 0(dark_pool_flow 暗盘口径)                       | 显式降级 |
| activity     | AI 机构活跃度 > 6        | 活跃度 > 大牛线 6.00(> 更佳线 12.00 标"更佳")               | 显式降级 |
| gs           | GS 在 G 区 + G 信号      | gs_strategy.zone == "G区" 且 signal == "G"(已确认)         | 显式降级 |
| wencai       | 问财关键词               | 传入关键词时: 命中问财列表才算过; 未传关键词 → 不参与筛选   | 显式降级 |

## 口径纪律
- **证据非建议**: 只回客观字段(条件 key/名称/通过态/证据值), 不含"建议买入"等主观措辞。
- **缺数据不出池**: 任一必需条件状态为 degraded(取数失败/无数据)→ `in_pool=False`。
- **问财降级**: 传了关键词但问财数据源不可用 → 该条件显式 degraded, 全池不出(不静默放宽)。
- 金额 = 元; 不做单位换算, 交出上游。

## 缓存
整个 screen 结果走 `src.web.cache.biz_cache`(L1 内存 + L2 Redis, key 前缀 `biz:`), TTL 60s。
禁业务代码裸连 Redis。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_CST = ZoneInfo("Asia/Shanghai")

# 条件状态
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_DEGRADED = "degraded"   # 缺数据/取数失败 → 显式降级(不出池)
STATUS_NA = "na"               # 未启用的可选条件(如未传问财关键词)

CONDITION_LABELS = {
    "dark_inflow": "暗盘资金流入",
    "activity": "AI机构活跃度>6",
    "gs": "GS在G区+G信号",
    "wencai": "问财关键词",
}

_DIGITS6 = re.compile(r"(\d{6})")


def _now() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


def _norm_symbol(raw) -> str:
    """任意代码样式(USZA002361 / 002361.SZ / 002361) → 6 位数字; 识别不了返回 ""。"""
    m = _DIGITS6.search(str(raw or "").strip().upper())
    return m.group(1) if m else ""


def _valid_symbols(raw: Sequence[str]) -> list[str]:
    """6 位数字 A 股代码, 去重保序。"""
    out: list[str] = []
    for s in raw or []:
        code = (s or "").strip()
        if code.isdigit() and len(code) == 6 and code not in out:
            out.append(code)
    return out


def _cond(key: str, status: str, evidence: dict, detail: str) -> dict:
    met: Optional[bool] = True if status == STATUS_PASS else (
        False if status == STATUS_FAIL else None
    )
    return {
        "key": key,
        "name": CONDITION_LABELS[key],
        "status": status,
        "met": met,
        "evidence": evidence,
        "detail": detail,
    }


def evaluate_one(
    symbol: str,
    *,
    dark_net: Optional[float] = None,
    activity: Optional[float] = None,
    gs_eval: Optional[dict] = None,
    wencai_hit: Optional[bool] = None,
    wencai_required: bool = False,
) -> dict:
    """单股逐条件判定(纯函数, 不取数)。返回带 `conditions` 明细的行。

    任一必需条件缺数据(状态 degraded)→ `in_pool=False`(显式降级, 不猜测补齐)。
    """
    conds: list[dict] = []

    # ① 暗盘资金流入(净额 > 0)
    if isinstance(dark_net, (int, float)):
        if dark_net > 0:
            conds.append(_cond("dark_inflow", STATUS_PASS, {"dark_net": dark_net},
                               f"暗盘净额 {dark_net:+.0f} 元(流入)"))
        else:
            conds.append(_cond("dark_inflow", STATUS_FAIL, {"dark_net": dark_net},
                               f"暗盘净额 {dark_net:+.0f} 元(未流入)"))
    else:
        conds.append(_cond("dark_inflow", STATUS_DEGRADED, {"dark_net": None},
                           "暗盘资金无数据(取数失败/未接入), 已降级"))

    # ② AI 机构活跃度 > 6(大牛线); > 12(更佳线)标"更佳"
    from src.core import thresholds as _th

    bull = _th.bull_line()
    top = _th.top_line()
    if isinstance(activity, (int, float)):
        better = activity >= top
        if activity > bull:
            conds.append(_cond(
                "activity", STATUS_PASS,
                {"activity": activity, "line": bull, "top_line": top, "better": better},
                f"活跃度 {activity:.2f} > {bull:g}" + ("(更佳, 站上更佳线 %g)" % top if better else ""),
            ))
        else:
            conds.append(_cond(
                "activity", STATUS_FAIL,
                {"activity": activity, "line": bull, "top_line": top, "better": False},
                f"活跃度 {activity:.2f} <= {bull:g}(未过大牛线)",
            ))
    else:
        conds.append(_cond("activity", STATUS_DEGRADED,
                           {"activity": None, "line": bull, "top_line": top, "better": False},
                           "AI 机构活跃度无数据(日K不足), 已降级"))

    # ③ GS 在 G 区 + G 信号
    if not isinstance(gs_eval, dict) or gs_eval.get("zone") is None:
        conds.append(_cond("gs", STATUS_DEGRADED,
                           {"zone": None, "signal": None},
                           "GS 无数据(日K不足), 已降级"))
    else:
        zone = gs_eval.get("zone")
        signal = gs_eval.get("signal")
        hit = (zone == "G区") and (signal == "G")
        conds.append(_cond(
            "gs", STATUS_PASS if hit else STATUS_FAIL,
            {"zone": zone, "signal": signal},
            f"GS 区={zone}, 信号={signal or '无'}" + ("" if hit else "(需 G区+G信号)"),
        ))

    # ④ 问财关键词(可选条件)
    if not wencai_required:
        conds.append(_cond("wencai", STATUS_NA, {"hit": None}, "未传关键词, 本条件不参与筛选"))
    elif isinstance(wencai_hit, bool):
        conds.append(_cond(
            "wencai", STATUS_PASS if wencai_hit else STATUS_FAIL,
            {"hit": wencai_hit}, "命中问财列表" if wencai_hit else "未在问财命中列表",
        ))
    else:
        conds.append(_cond("wencai", STATUS_DEGRADED, {"hit": None},
                           "问财数据源不可用, 已显式降级(不静默放宽)"))

    in_pool = all(c["status"] in (STATUS_PASS, STATUS_NA) for c in conds)
    return {
        "symbol": symbol,
        "in_pool": in_pool,
        "conditions": conds,
        "dark_net": dark_net if isinstance(dark_net, (int, float)) else None,
        "activity": activity if isinstance(activity, (int, float)) else None,
        "activity_level": (_activity_level_of(activity) if isinstance(activity, (int, float)) else None),
        "better": bool(isinstance(activity, (int, float)) and activity >= top),
        "gs_zone": gs_eval.get("zone") if isinstance(gs_eval, dict) else None,
        "gs_signal": gs_eval.get("signal") if isinstance(gs_eval, dict) else None,
    }


def _activity_level_of(activity: float) -> Optional[str]:
    """活跃度 → 档位(复用语义层, 含 >12 更佳档)。"""
    from src.core.ai_activity import activity_of_value

    return activity_of_value(activity).get("level")


# ── 取数封装(便于测试 monkeypatch, 不触真网络) ──────────────────────────────
def _bars_of(symbol: str) -> list[dict]:
    from src.core.decision_pioneer import fetch_bars

    return fetch_bars(symbol, "CN", days=60)


def _dark_net_of(symbol: str) -> Optional[float]:
    """暗盘净额(元); 取不到返回 None(调用方显式降级)。"""
    try:
        from src.core.dark_pool_flow import compute_pool_flow

        pool = compute_pool_flow(symbol) or {}
        dark = pool.get("dark") or {}
        net = dark.get("net")
        return round(net, 2) if isinstance(net, (int, float)) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("聚宝盆暗盘取数失败 %s: %s", symbol, e)
        return None


def _wencai_symbols(keywords: str) -> dict:
    """跑问财关键词 → {available, symbols:set, note}。假流/失败 → available=False(显式降级)。"""
    from src.collectors.wencai import run_wencai

    res = run_wencai(keywords) or {}
    avail = bool(res.get("available"))
    syms: set[str] = set()
    for r in res.get("rows") or []:
        s = _norm_symbol(r.get("symbol") or r.get("代码") or r.get("股票代码"))
        if s:
            syms.add(s)
    return {"available": avail, "symbols": syms, "note": res.get("note") or ""}


def screen(symbols: Sequence[str], wencai_keywords: Optional[str] = None) -> dict:
    """聚宝盆选股: 逐条件 AND 筛选, 输出**逐条件通过/未过明细**(证据非建议)。

    Args:
        symbols: 6 位 A 股代码列表
        wencai_keywords: 可选的问财自然语言条件; 传入即作为筛选条件之一(不可用→显式降级)

    Returns:
        {generated_at, universe, scanned, in_pool, wencai:{...}, filters:{...}, rows:[...], note}
    """
    codes = _valid_symbols(symbols)
    kw = (wencai_keywords or "").strip()
    wencai_required = bool(kw)

    wencai_info = {"provided": wencai_required, "available": None, "hits": 0, "note": ""}
    wencai_syms: set[str] = set()
    if wencai_required:
        try:
            wi = _wencai_symbols(kw)
        except Exception as e:  # noqa: BLE001
            logger.warning("聚宝盆问财取数失败: %s", e)
            wi = {"available": False, "symbols": set(), "note": str(e)}
        wencai_syms = wi["symbols"]
        wencai_info = {
            "provided": True,
            "available": bool(wi["available"]),
            "hits": len(wencai_syms),
            "note": wi.get("note") or "",
        }

    rows: list[dict] = []
    for code in codes:
        bars: list[dict] = []
        try:
            bars = _bars_of(code) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("聚宝盆日K取数失败 %s: %s", code, e)

        from src.core.ai_activity import eval_activity
        from src.core.gs_strategy import eval_gs

        try:
            activity = (eval_activity(bars) or {}).get("activity") if bars else None
        except Exception:  # noqa: BLE001
            activity = None
        try:
            gs_eval = eval_gs(bars) if bars else None
        except Exception:  # noqa: BLE001
            gs_eval = None

        dark_net = _dark_net_of(code)

        # 问财命中: 传了关键词但源不可用 → 三者都置 None(显式降级, 已在下游判 degraded)
        wencai_hit: Optional[bool] = None
        if wencai_required and wencai_info["available"]:
            wencai_hit = code in wencai_syms

        rows.append(evaluate_one(
            code, dark_net=dark_net, activity=activity, gs_eval=gs_eval,
            wencai_hit=wencai_hit, wencai_required=wencai_required,
        ))

    # 排序: 入池优先, 其次活跃度降序
    rows.sort(key=lambda r: (not r["in_pool"], -(r["activity"] or 0.0)))

    in_pool = sum(1 for r in rows if r["in_pool"])
    note = None
    if wencai_required and not wencai_info["available"]:
        note = "问财数据源不可用: 问财条件显式降级, 全池不出(不静默放宽)"
    return {
        "generated_at": _now(),
        "universe": len(codes),
        "scanned": len(rows),
        "in_pool": in_pool,
        "wencai": wencai_info,
        "filters": {
            "dark_inflow": "暗盘净额>0",
            "activity": "活跃度>6(>12更佳)",
            "gs": "G区+G信号",
            "wencai": kw if wencai_required else "未启用",
        },
        "rows": rows,
        "note": note,
    }



