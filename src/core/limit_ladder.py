# -*- coding: utf-8 -*-
"""连板梯队(v0.5.81, 老板: "连扳梯队也要做")。

## 口径
通达信式"连板天梯": 每个交易日按**连板高度**分组列出个股。连板数不信任
`limit_up_events.limit_days`(该列从未落库), 而是**从事件表自己推**:
某 symbol 在 d 日收盘封板(`is_sealed_close=1`), 且 d 的前一个**有事件的交易日**
(即表里存在的日期序列)它也封板, 则连板 +1 —— 用表内日期序列而非日历日, 避免
节假日/停牌把连板打断。

只统计**收盘封板**(touch 未封不算连板, 与 market_phase 的 sealed 口径一致)。

## 输出
`ladder_window(dates, events)` → 每日 `{date, rows: [{boards, names, sealed_n}]}`
按 boards 降序; 空日返回 `rows: []`(不编造)。纯函数, 不碰库。
"""
from __future__ import annotations

from typing import Iterable


def _sealed_by_date(events: Iterable[dict]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for e in events:
        if not e.get("is_sealed_close"):
            continue
        d, sym = str(e.get("trade_date") or ""), str(e.get("symbol") or "")
        if not d or not sym:
            continue
        out.setdefault(d, set()).add(sym)
    return out


def boards_for_date(dates: list[str], sealed: dict[str, set[str]], date: str) -> dict[str, int]:
    """date 当日各 symbol 的连板数: 沿表内日期序列往前数连续封板日。"""
    idx = dates.index(date) if date in dates else -1
    if idx < 0:
        return {}
    out: dict[str, int] = {}
    for sym in sealed.get(date, set()):
        n = 1
        i = idx - 1
        while i >= 0 and sym in sealed.get(dates[i], set()):
            n += 1
            i -= 1
        out[sym] = n
    return out


def ladder_window(dates: list[str], events: Iterable[dict],
                  names: dict[str, str] | None = None) -> list[dict]:
    """窗口内每日梯队。`names` 缺省时名字回退为代码(不猜中文名)。"""
    names = names or {}
    sealed = _sealed_by_date(events)
    out: list[dict] = []
    for d in dates:
        boards = boards_for_date(dates, sealed, d)
        grouped: dict[int, list[str]] = {}
        for sym, n in boards.items():
            grouped.setdefault(n, []).append(sym)
        rows = []
        for b in sorted(grouped, reverse=True):
            syms = sorted(grouped[b])
            rows.append({
                "boards": b,
                "codes": syms,
                "names": [names.get(s, s) for s in syms],
                "sealed_n": len(sealed.get(d, set())),
            })
        out.append({"date": d, "rows": rows})
    return out


def touched_by_date(events: Iterable[dict]) -> dict[str, set[str]]:
    """当日**触板**(含封住与炸板)的 symbol 集合 —— 事件表每行即一次触板。"""
    out: dict[str, set[str]] = {}
    for e in events:
        d, sym = str(e.get("trade_date") or ""), str(e.get("symbol") or "")
        if not d or not sym:
            continue
        out.setdefault(d, set()).add(sym)
    return out


def finalize_marks(dates: list[str], events: Iterable[dict]) -> dict[str, dict]:
    """收盘定型的炸板/断板(v0.5.85, spec §3.2)。

    blown  = 当日触板但收盘未封(is_sealed_close=False);
    broken = 上一表日收盘封板且 boards>=2, 当日**未触板**;
             昨首板今未续只算淘汰, 不标断板;
    prev_boards = 上一表日的连板数(boards_for_date 口径), 无则 None。
    纯函数; 空事件 → 每日 {"blown": [], "broken": []}(不编)。
    """
    events = list(events)
    sealed = _sealed_by_date(events)
    touched = touched_by_date(events)
    names = {str(e["symbol"]): e.get("name") for e in events if e.get("name")}
    out: dict[str, dict] = {}
    for i, d in enumerate(dates):
        prev = dates[i - 1] if i > 0 else None
        prev_boards = boards_for_date(dates, sealed, prev) if prev else {}
        blown = [
            {"symbol": s, "name": names.get(s), "prev_boards": prev_boards.get(s)}
            for s in sorted(touched.get(d, set()) - sealed.get(d, set()))
        ]
        broken = [
            {"symbol": s, "name": names.get(s), "prev_boards": b}
            for s, b in sorted(prev_boards.items())
            if b >= 2 and s not in touched.get(d, set())
        ]
        out[d] = {"blown": blown, "broken": broken}
    return out


def attach_candles(stocks: list[dict], date: str, ohlc: dict) -> list[dict]:
    """给逐股明细挂当日K(v0.5.85, spec §6); 缺 OHLC → candle=None(不编影线)。

    ohlc 键为 (date, symbol) → {"o","h","l","c"}。
    """
    return [{**s, "candle": ohlc.get((date, s["symbol"]))} for s in stocks]
