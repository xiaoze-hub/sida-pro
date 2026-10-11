#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""首封时间×封成比 强度因子 历史回测(只读, 2026-10-11 胜率创新 P0-1)。

口径见 docs/research/首封时间封成比_影子验证_模板.md。本脚本**只读**:
- 事件源: `limit_up_events`(已落库涨停事件, 含 first_time / max_seal_amount / circ_mv /
  open_count 列 —— 多数历史行这些列为空, 由影子运行逐日补);
- 收益源: 日K(`demon_factors._tq_bars_direct`, 300 根), close-to-close T+1 / T+3;
- 标签: 直接复用 `src.core.limit_up_strength.strength_label`(口径与影子表完全一致)。

诚实铁律(**能跑多少跑多少, 缺口显式**):
- 首封时间与封成比任一缺 → 该样本计入 gap(不进任何桶), **绝不臆造**;
- 每组样本 < 30 → 打印「样本不足, 不出结论」, 不给方向性判断;
- 命中率给 Wilson 95% 置信区间; 结论只写「统计上 X 组 vs Y 组」, 禁「建议买入」。

用法:
    python scripts/limit_strength_backtest.py [--horizon 1 3] [--min-sample 30] [--max-days 60]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text  # noqa: E402


def load_events(max_days: int) -> list[dict]:
    """读最近 max_days 个交易日的涨停事件(只读)。表不存在/库不可用 → 空(调用方显式说明)。"""
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        try:
            days = db.execute(
                text("SELECT DISTINCT trade_date FROM limit_up_events ORDER BY trade_date DESC LIMIT :n"),
                {"n": max_days},
            ).fetchall()
        except Exception as e:  # noqa: BLE001 表未迁移/库为空 → 显式空, 不 traceback
            print(f"读 limit_up_events 失败(视为无数据): {e}")
            return []
        dates = [str(d[0]) for d in days]
        if not dates:
            return []
        rows = db.execute(
            text(
                "SELECT trade_date, symbol, name, first_time, max_seal_amount, circ_mv, "
                "open_count, is_sealed_close FROM limit_up_events WHERE trade_date >= :a"
            ),
            {"a": min(dates)},
        ).fetchall()
        return [dict(r._mapping) for r in rows]
    finally:
        db.close()


def label_of(ev: dict) -> dict:
    """事件行 → 强度标签(口径同影子表)。"""
    from src.core.limit_up_strength import strength_label

    return strength_label(
        first_time=ev.get("first_time"),
        seal_amount=ev.get("max_seal_amount"),
        circulating_mktcap=ev.get("circ_mv"),
        open_times=ev.get("open_count"),
    )


def _closes(symbol: str, cache: dict) -> dict[str, float]:
    if symbol not in cache:
        try:
            from src.core.demon_factors import _tq_bars_direct

            bars = _tq_bars_direct(symbol, 300)
            cache[symbol] = {
                str(b.get("date")): float(b["close"])
                for b in bars
                if b.get("close") and b.get("date")
            }
        except Exception:  # noqa: BLE001
            cache[symbol] = {}
    return cache[symbol]


def _forward_close(cs: dict[str, float], date: str, h: int) -> float | None:
    """T+h: 按自然日顺延找有收盘的最近交易日(最多顺延 15 天)。"""
    from datetime import datetime, timedelta

    d0 = datetime.strptime(date, "%Y%m%d")
    seen = 0
    for k in range(1, 16):
        key = (d0 + timedelta(days=k)).strftime("%Y%m%d")
        if key in cs:
            seen += 1
            if seen >= h:
                return cs[key]
    return None


def wilson_ci(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 95% 置信区间(比例, 小样本更稳)。返回 (lo, hi) 百分数。"""
    if n <= 0:
        return (0.0, 0.0)
    p = hits / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (max(0.0, (centre - margin)) * 100, min(1.0, centre + margin) * 100)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, nargs="+", default=[1, 3], help="回看收益期(默认 T+1 T+3)")
    ap.add_argument("--min-sample", type=int, default=30, help="每组出结论的最小样本(默认 30)")
    ap.add_argument("--max-days", type=int, default=60, help="最多回测的交易日数(默认 60)")
    args = ap.parse_args()

    events = load_events(args.max_days)
    if not events:
        print("limit_up_events 无数据: 先回填(POST /api/market/phase/sync 或 limit_up_backfill)。")
        return

    # 只统计收盘封板的(与梯队口径一致)
    sealed = [e for e in events if e.get("is_sealed_close")]
    groups: dict[str, list[dict]] = defaultdict(list)
    gap_missing_first = gap_missing_ratio = 0
    for e in sealed:
        res = label_of(e)
        if res["status"] != "ok":
            if "first_time" in res["missing"]:
                gap_missing_first += 1
            if "seal_ratio" in res["missing"]:
                gap_missing_ratio += 1
            groups["no_data"].append(e)
        else:
            groups[res["label"]].append(e)

    total = len(sealed)
    print(f"样本: 收盘封板事件 {total} 条(窗口 {args.max_days} 交易日)")
    print(
        f"缺口: 缺首封时间 {gap_missing_first} ({gap_missing_first / total * 100:.1f}%), "
        f"缺封成比 {gap_missing_ratio} ({gap_missing_ratio / total * 100:.1f}%) "
        "—— 不进桶, 不臆造"
    )
    if gap_missing_first / max(total, 1) > 0.5:
        print("⚠️ 历史首封时间大面积缺失 → **结论推迟到影子运行满 20 交易日后**(见验证模板 §3)。")

    cache: dict[str, dict] = {}
    for h in args.horizon:
        print(f"\n=== T+{h} 收益(close-to-close) ===")
        for group in ("S", "A", "B", "C", "no_data"):
            evs = groups.get(group) or []
            rets: list[float] = []
            hits = 0
            for e in evs:
                cs = _closes(str(e["symbol"]), cache)
                c0 = cs.get(str(e["trade_date"]))
                if not c0:
                    continue
                c_h = _forward_close(cs, str(e["trade_date"]), h)
                if c_h is None:
                    continue
                r = (c_h - c0) / c0 * 100
                rets.append(r)
                if r > 0:
                    hits += 1
            n = len(rets)
            if n == 0:
                print(f"  {group}: 无可用样本")
                continue
            avg = sum(rets) / n
            hit_rate = hits / n * 100
            lo, hi = wilson_ci(hits, n)
            tag = "" if n >= args.min_sample else "  ← 样本不足, 不出结论"
            print(
                f"  {group}: n={n} 均值={avg:+.2f}% 命中率={hit_rate:.1f}% "
                f"(Wilson95% {lo:.1f}~{hi:.1f}%){tag}"
            )

    print("\n结论口径: 仅统计组间差异; 禁『建议买入』。样本<{} 不出方向性结论。".format(args.min_sample))


if __name__ == "__main__":
    main()
