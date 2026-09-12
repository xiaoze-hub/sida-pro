# -*- coding: utf-8 -*-
"""题材轮动统计(v0.5.81)。

## 为什么
题材情绪榜原先只取**最新一天**的 Top-N 当行集合(`_board_data` 里
`SELECT * WHERE trade_date = latest`), 于是"昨天还热、今天掉出榜"的题材**整行消失**,
轮动看不见 —— 老板原话: "题材不能做成固定的, 题材是轮动的"。

## 口径
- 每日 Top-K 按当日情绪分取前 K 名(K 默认 10, 与榜单 top 参数解耦);
- **新进** = 当日在 Top-K 且前一日不在; **退榜** = 前一日在、当日不在;
- 行集合 = 窗口内每日 Top-K 的**并集**(再并入最新一天的全部行), 所以退榜题材仍留一行,
  用它自己的分数曲线展示"怎么退的";
- 每个题材附 `top_days`(窗口内在榜天数)/`first_top_date`/`last_top_date`/`in_top_today`,
  前端据此打「新」「退」标与在榜天数。

纯函数, 不碰库; 输入是 `theme_mood_daily` 读出来的 (date, code, name, score) 行。
"""
from __future__ import annotations

from typing import Iterable


def daily_top_sets(hist: Iterable[dict], top_k: int = 10) -> dict[str, list[str]]:
    """date → 当日情绪分前 top_k 的 block_code(降序)。score 为 None 的行不参与。"""
    by_date: dict[str, list[tuple[float, str]]] = {}
    for h in hist:
        score = h.get("score")
        code = h.get("block_code")
        if score is None or not code:
            continue
        by_date.setdefault(str(h.get("trade_date")), []).append((float(score), str(code)))
    out: dict[str, list[str]] = {}
    for d, pairs in by_date.items():
        pairs.sort(key=lambda p: (-p[0], p[1]))
        out[d] = [c for _, c in pairs[: max(1, int(top_k))]]
    return out


def rotation_series(dates: list[str], top_sets: dict[str, list[str]]) -> list[dict]:
    """按日期升序给出每日新进/退榜。首日无前日基准 → new/exit 都为空(不猜)。"""
    seq: list[dict] = []
    prev: set[str] | None = None
    for d in dates:
        cur = set(top_sets.get(d, []))
        if prev is None:
            new, gone = set(), set()
        else:
            new, gone = cur - prev, prev - cur
        seq.append({
            "date": d,
            "new_n": len(new),
            "exit_n": len(gone),
            "new_codes": sorted(new),
            "exit_codes": sorted(gone),
        })
        prev = cur
    return seq


def membership_flags(dates: list[str], top_sets: dict[str, list[str]]) -> dict[str, dict]:
    """block_code → {top_days, first_top_date, last_top_date, in_top_today}。"""
    out: dict[str, dict] = {}
    latest = dates[-1] if dates else None
    for d in dates:
        for code in top_sets.get(d, []):
            rec = out.setdefault(code, {"top_days": 0, "first_top_date": None,
                                        "last_top_date": None, "in_top_today": False})
            rec["top_days"] += 1
            rec["first_top_date"] = rec["first_top_date"] or d
            rec["last_top_date"] = d
            if d == latest:
                rec["in_top_today"] = True
    return out
