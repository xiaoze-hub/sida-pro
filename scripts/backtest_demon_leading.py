"""妖股领先效应回测(批次B, 2026-09-06 28号)。

验证老板假设: "板块轮到时, 妖股(近一年涨停≥N次)涨得最快最多"。

MVP 口径(诚实标注):
- 题材事件日代理 = 全市场涨停家数 > 1.5 × 前 5 个涨停日均值的交易日
  (板块级事件日待题材传导链接入后替换为板块口径);
- 妖股组 = 事件日前 250 自然日内封板次数 ≥ DEMON_N 的股票;
- 对照组 = 同一事件日其余涨停股(非妖股组);
- 收益 = 事件日收盘 → T+1/T+5 收盘(close-to-close, 复权口径由 fetch_bars 决定)。
  涨停股 T+1 买不进的偏差(一字板)未剔除, 报告标注。

前置: 先跑 POST /api/demon-pool/backfill 或 limit_up_backfill.backfill_all()。
用法: python scripts/backtest_demon_leading.py [--max-events 40] [--demon-n 10]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text  # noqa: E402


def load_events() -> list[dict]:
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT trade_date, symbol, is_sealed_close FROM limit_up_events"
                " WHERE trade_date >= (SELECT MAX(trade_date) FROM limit_up_events)"
                " ORDER BY trade_date"
            )
        ).fetchall()
        # MAX(trade_date) 只取最近一年窗口: 表内若有更老数据按日期字符串比较近似
        return [dict(r._mapping) for r in rows]
    finally:
        db.close()


def demon_universe_at(events_by_date: dict[str, list[dict]], date: str, demon_n: int, window: int = 250) -> set[str]:
    """事件日前 250 自然日内封板次数 >= demon_n 的股票集合(用事件表近似近一年)。"""
    from datetime import datetime, timedelta

    d0 = datetime.strptime(date, "%Y%m%d")
    start = (d0 - timedelta(days=window)).strftime("%Y%m%d")
    cnt: dict[str, int] = defaultdict(int)
    for d, evs in events_by_date.items():
        if start <= d < date:
            for e in evs:
                if e.get("is_sealed_close"):
                    cnt[e["symbol"]] += 1
    return {s for s, c in cnt.items() if c >= demon_n}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-events", type=int, default=40, help="最多回测的事件日数")
    ap.add_argument("--demon-n", type=int, default=10, help="妖股门槛: 前250自然日封板次数")
    args = ap.parse_args()

    events = load_events()
    if not events:
        print("limit_up_events 无数据: 先回填(POST /api/demon-pool/backfill)")
        return
    events_by_date: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        events_by_date[e["trade_date"]].append(e)
    dates = sorted(events_by_date)

    # 事件日: 涨停家数 > 1.5 × 前 5 个涨停日均家数
    event_days = []
    for i, d in enumerate(dates):
        if i < 5:
            continue
        prev = [len(events_by_date[x]) for x in dates[max(0, i - 5):i]]
        base = sum(prev) / len(prev)
        if len(events_by_date[d]) > 1.5 * base:
            event_days.append(d)
    event_days = event_days[-args.max_events:]

    if not event_days:
        print("窗口内无满足阈值的事件日, 放宽 1.5 系数或先回填更多历史。")
        return

    from src.core.decision_pioneer import fetch_bars

    close_cache: dict[str, dict[str, float]] = {}

    def closes(symbol: str) -> dict[str, float]:
        if symbol not in close_cache:
            bars = fetch_bars(symbol, "CN", days=300)
            close_cache[symbol] = {str(b.get("date")).replace("-", ""): float(b["close"]) for b in bars if b.get("close")}
        return close_cache[symbol]

    stat: dict[int, dict[str, list[float]]] = {1: {"demon": [], "other": []}, 5: {"demon": [], "other": []}}
    skipped = 0
    for d in event_days:
        demons = demon_universe_at(events_by_date, d, args.demon_n)
        if not demons:
            continue
        for e in events_by_date[d]:
            horizon_stats = "demon" if e["symbol"] in demons else "other"
            cs = closes(e["symbol"])
            c0 = cs.get(d)
            if c0 is None:
                skipped += 1
                continue
            for h in (1, 5):
                from datetime import datetime as _dt, timedelta as _td

                # T+h: 按自然日顺延找有收盘的最近日(简化: 逐日+1 最多 10 天)
                d0 = _dt.strptime(d, "%Y%m%d")
                c_h = None
                for k in range(1, 15):
                    key = (d0 + _td(days=k)).strftime("%Y%m%d")
                    if key in cs:
                        c_h = cs[key]
                        if k >= h:
                            break
                if c_h is None:
                    continue
                ret = (c_h - c0) / c0 * 100
                stat[h][horizon_stats].append(ret)

    print(f"事件日数={len(event_days)} 妖股门槛=前250自然日封板≥{args.demon_n}次 跳过(无K线)={skipped}")
    print("口径: 事件日=全市场涨停家数>1.5×前5日均值的代理; T+1 收盘买入偏差(一字板)未剔除")
    for h in (1, 5):
        d_ = stat[h]["demon"]
        o_ = stat[h]["other"]
        if not d_ or not o_:
            print(f"T+{h}: 样本不足 demon={len(d_)} other={len(o_)}")
            continue
        avg = lambda xs: sum(xs) / len(xs)  # noqa: E731
        win = lambda xs: sum(1 for x in xs if x > 0) / len(xs) * 100  # noqa: E731
        print(
            f"T+{h}: 妖股组 n={len(d_)} 均值={avg(d_):+.2f}% 胜率={win(d_):.1f}% | "
            f"对照组(其余涨停股) n={len(o_)} 均值={avg(o_):+.2f}% 胜率={win(o_):.1f}% | "
            f"超额={avg(d_) - avg(o_):+.2f}pct"
        )


if __name__ == "__main__":
    main()
