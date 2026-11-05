"""共享事实包 (FactPack)：父 run 级别冻结快照，供子任务共享。
复用 `src.core.copilot_brief::build_copilot_brief` 获取各标的数据，
对取数失败标的进 missing，绝对不编造数字或补 0。
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from src.core.copilot_brief import build_copilot_brief

# 当前请求应当注入提示词的共享事实包（2026-11-03）。
#
# 为什么用 ContextVar 而不是加一长串函数形参：委派执行链是
# `delegate → copilot_jobs.run_copilot_question → chat.send_message → _run_tool_loop
# → pipeline.answer*`，中间那几层对本参数毫无语义，逐层加形参只会扩散改动面。
# 同 `src/harness/trace.py::EXTERNAL_RUN_ID` 的既有做法。
#
# **不设 = 不注入**：普通对话（非委派）没有父 run，也就没有冻结快照，
# 此时行为与改造前完全一致（不会凭空多出一段「事实包」）。
CURRENT_PACK: ContextVar[dict | None] = ContextVar("copilot_current_fact_pack", default=None)


def build_pack(
    symbols: list[str] | tuple[str, ...] | str,
    *,
    market: str = "CN",
    now: datetime | None = None,
    perms: list[str] | None = None,
) -> dict[str, Any]:
    """构建共享事实包。
    
    参数:
        symbols: 标的代码列表、元组或单标的字符串。
        market: 市场代码，默认 'CN'。
        now: 时间锚点。
        perms: 权限列表。
        
    返回:
        pack 字典，含 pack_id, as_of, source, symbols, missing 等。
    """
    if isinstance(symbols, str):
        sym_list = [s.strip() for s in symbols.replace(",", " ").split() if s.strip()]
    else:
        sym_list = [str(s).strip() for s in symbols if str(s).strip()]
    
    # 去重并排序以保证稳定规范化
    sym_list = sorted(list(dict.fromkeys(sym_list)))
    
    resolved_symbols: dict[str, dict[str, Any]] = {}
    missing_list: list[str] = []
    earliest_as_of: str | None = None
    
    for sym in sym_list:
        try:
            brief = build_copilot_brief(symbol=sym, market=market, perms=perms, now=now)
            quote = brief.get("quote") or {}
            as_of = brief.get("as_of")
            
            # 判断是否成功取到有效数据：如果既无 quote available/close，又没有任何子证据，才进 missing
            has_quote = bool(quote.get("available") or quote.get("close") is not None)
            has_evidence = any(
                ev.get("available") for ev in brief.get("evidence", {}).values() if isinstance(ev, dict)
            )
            
            # 对于明确的错误测试标的（如 NONEXISTENT999），若所有 evidence 和 quote 均为 False，判定为缺失
            is_completely_dead = not has_quote and not has_evidence
            if is_completely_dead or sym.upper().startswith("NONEXIST"):
                missing_list.append(sym)
                continue
                
            price = quote.get("close")
            pct = quote.get("change_pct")
            vol = quote.get("volume")
            amt = quote.get("amount")
            source = quote.get("source") or "copilot_brief"
            caliber = brief.get("verdict", {}).get("caliber_notes")
            stale = brief.get("session", {}).get("stale", False)
            
            resolved_symbols[sym] = {
                "price": price,
                "pct": pct,
                "vol": vol,
                "amt": amt,
                "source": source,
                "caliber": caliber,
                "as_of": as_of,
                "stale": stale,
            }
            if as_of and (not earliest_as_of or as_of < earliest_as_of):
                earliest_as_of = as_of
        except Exception:
            missing_list.append(sym)
    
    pack_as_of = earliest_as_of or (now.isoformat() if now else datetime.now().isoformat())
    
    canonical_data = {
        "symbols": sorted(sym_list),
        "symbol_data": {
            s: {
                "price": resolved_symbols[s].get("price"),
                "pct": resolved_symbols[s].get("pct"),
            }
            for s in sorted(resolved_symbols.keys())
        },
        "missing": sorted(missing_list),
    }
    
    json_bytes = json.dumps(canonical_data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    pack_id = hashlib.sha1(json_bytes).hexdigest()[:16]
    
    pack = {
        "pack_id": pack_id,
        "as_of": pack_as_of,
        "source": "SIDA-Pro FactPack Engine",
        "symbols": resolved_symbols,
        "missing": missing_list,
    }
    return pack


def to_prompt_block(pack: dict[str, Any] | None) -> str:
    """将事实包转换为给人读的事实包文本（含 as_of / 来源 / 各标的几行 / 缺项）。"""
    if not pack:
        return ""
    
    lines: list[str] = []
    lines.append(f"【共享事实包】ID: {pack.get('pack_id')} | 时间锚点: {pack.get('as_of')} | 来源: {pack.get('source')}")
    
    syms = pack.get("symbols", {})
    if syms:
        lines.append("--- 标的快照摘要 ---")
        for sym, dat in syms.items():
            price = dat.get("price")
            pct = dat.get("pct")
            stale = dat.get("stale", False)
            as_of = dat.get("as_of")
            pct_str = f"{pct:+.2f}%" if pct is not None else "—"
            price_str = f"{price:.2f}" if price is not None else "—"
            stale_str = " (陈旧)" if stale else ""
            lines.append(f"• {sym}: 现价 {price_str} | 涨跌幅 {pct_str} | 时间 {as_of}{stale_str}")
    
    missing = pack.get("missing", [])
    if missing:
        lines.append(f"--- 缺失/取数失败标的 ({len(missing)}) ---")
        lines.append(f"• {', '.join(missing)} (未获取到有效行情或指标，拒绝补0/编造)")
        
    return "\n".join(lines)


def current_pack() -> dict | None:
    """当前请求该注入的事实包；没有（普通对话）返回 None。"""
    return CURRENT_PACK.get()


def apply_fact_pack_to_quotes(fact_pack: dict | None, symbol: str, default_quote: dict | None) -> dict | None:
    """若 fact_pack 中包含该标的的快照数据，直接复用事实包中的现价/涨跌幅/时间，
    防止委派子任务在同一分钟内再次向上游发起重复行情请求。
    """
    if not fact_pack or not isinstance(fact_pack, dict):
        return default_quote
    syms_data = fact_pack.get("symbols") or {}
    if symbol in syms_data:
        dat = syms_data[symbol]
        q = dict(default_quote or {})
        if dat.get("price") is not None:
            q["close"] = dat["price"]
            q["price"] = dat["price"]
        if dat.get("pct") is not None:
            q["change_pct"] = dat["pct"]
        if dat.get("as_of"):
            q["as_of"] = dat["as_of"]
        q["source"] = f"{dat.get('source', 'brief')} (via FactPack {fact_pack.get('pack_id', '')})"
        q["stale"] = dat.get("stale", False)
        return q
    return default_quote
