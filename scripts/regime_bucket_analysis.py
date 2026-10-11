#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""胜率证据层验证脚本(2026-10-11): 决策账本**分桶对比** —— 条件化是否比混算提胜率。

回答: 决策账本 resonance3 的全状态混算 T+1 命中率是 **53.85%** —— 但"53.85%"是不是被
状态平均掩盖了? 本脚本把每个信号按其**信号当日市场温度**(封板成功率 / 炸板比, 取自
market_sentiment_daily)与**情绪周期 regime**分桶, 逐桶算 T+1/3/5 命中率, 与混算对照。

口径(与 decision_log 一致):
  · 命中 = T+n 收益 > 0(平盘记未命中); 只统计已回填行(hit_tN IS NOT NULL), 缺失不进分母;
  · 封板成功率 = seal_success_money / (seal_success_money + seal_fail_money);
    炸板比 = limit_up_open_count / limit_up_count;
  · 桶阈值走 src.core.thresholds(seal_rate_tighten / seal_rate_loosen / blow_rate_tighten);
  · 样本 < min_sample 的桶标记『样本不足』, 不给命中率(不拿小样本造数)。

用法:
  # 生产 PG(只读) / 任一可读库
  SIDA_DB_URL="postgresql+psycopg2://user:pw@host:5432/sida" \\
    python scripts/regime_bucket_analysis.py --days 400 --min-sample 30
  # 本地 sqlite
  python scripts/regime_bucket_analysis.py --db sqlite:///data/panwatch.db

只读: 全程 SELECT, 不写库。输出为 Markdown 表格, 便于贴进 docs/research/ 报告。
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402


def _dsn(args) -> str:
    if args.db:
        return args.db
    for env in ("DATABASE_URL_READ", "SIDA_DB_URL", "DATABASE_URL_WRITE"):
        v = os.environ.get(env)
        if v:
            return v
    return "sqlite:///data/panwatch.db"


def _rate(rows, idx) -> tuple[int, float | None]:
    vals = [r[idx] for r in rows if r[idx] is not None]
    if not vals:
        return 0, None
    return len(vals), sum(vals) / len(vals) * 100.0


def _bucket_label(rate: float | None, t_tighten: float, t_loosen: float) -> str:
    if rate is None:
        return "无数据"
    if rate < t_tighten:
        return "冷(封板成功率<收紧线)"
    if rate >= t_loosen:
        return "热(封板成功率≥放宽线)"
    return "中(收紧线~放宽线)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help="SQLAlchemy DSN(缺省读环境变量/本地 sqlite)")
    ap.add_argument("--kind", default="resonance3", help="信号类型(默认 resonance3)")
    ap.add_argument("--days", type=int, default=400, help="回看天数(按信号产生日)")
    ap.add_argument("--min-sample", type=int, default=30, help="低于此样本量不给命中率")
    ap.add_argument("--sentiment-lookback", type=int, default=3,
                    help="信号日无当日温度时向前取最近交易日的最大天数(0=不向前)")
    args = ap.parse_args()

    from src.core import thresholds
    t_tighten = thresholds.value("seal_rate_tighten")
    t_loosen = thresholds.value("seal_rate_loosen")
    t_blow = thresholds.value("blow_rate_tighten")

    eng = create_engine(_dsn(args))
    since = (date.today() - timedelta(days=args.days)).isoformat()
    since_c = since.replace("-", "")

    with eng.connect() as conn:
        dl = conn.execute(
            text(
                "SELECT trade_date, hit_t1, hit_t3, hit_t5, COALESCE(NULLIF(regime,''),'unknown') AS reg "
                "FROM decision_log WHERE signal_kind = :k AND "
                "((length(trade_date)=8 AND trade_date>=:sc) OR (length(trade_date)=10 AND trade_date>=:s))"
            ),
            {"k": args.kind, "s": since, "sc": since_c},
        ).fetchall()
        sent = conn.execute(
            text(
                "SELECT trade_date, limit_up_count, limit_up_open_count, "
                "seal_success_money, seal_fail_money FROM market_sentiment_daily WHERE market='CN' "
                "ORDER BY trade_date"
            )
        ).fetchall()

    def norm(d: str) -> str:
        s = str(d)
        return f"{s[:4]}-{s[4:6]}-{s[6:]}" if len(s) == 8 and s.isdigit() else s

    # 温度按日(封板成功率 / 炸板比)
    day_rate: dict[str, float | None] = {}
    day_blow: dict[str, float | None] = {}
    for td, luc, luo, ss, sf in sent:
        ss_f = float(ss) if ss is not None else None
        sf_f = float(sf) if sf is not None else None
        if ss_f is not None or sf_f is not None:
            denom = (ss_f or 0.0) + (sf_f or 0.0)
            day_rate[norm(td)] = ((ss_f or 0.0) / denom) if denom > 0 else None
        if luc and luo is not None and float(luc) > 0:
            day_blow[norm(td)] = float(luo) / float(luc)

    sent_days = sorted(day_rate)
    import bisect

    def temp_for(day_iso: str) -> float | None:
        i = bisect.bisect_right(sent_days, day_iso) - 1
        if i < 0:
            return None
        picked = sent_days[i]
        if args.sentiment_lookback:
            gap = (date.fromisoformat(day_iso) - date.fromisoformat(picked)).days
            if gap > args.sentiment_lookback:
                return None
        return day_rate[picked]

    rows = [(norm(td), h1, h3, h5, reg) for td, h1, h3, h5, reg in dl]
    mixed = {n: _rate(rows, i) for n, i in (("T+1", 1), ("T+3", 2), ("T+5", 3))}

    print(f"# 决策账本分桶对比: {args.kind} (since {since})\n")
    print(f"- 样本行数: {len(rows)}；min_sample={args.min_sample}")
    print(f"- 阈值: 封板成功率收紧线={t_tighten} 放宽线={t_loosen} 炸板比收紧线={t_blow}")
    print("\n## 混算(全部状态平均)\n")
    print("| 档 | 样本 | 命中率 |\n|---|---|---|")
    for n in ("T+1", "T+3", "T+5"):
        cnt, r = mixed[n]
        print(f"| {n} | {cnt} | {'—' if r is None else f'{r:.2f}%'} |")

    # 按封板成功率分桶
    buckets: dict[str, list] = {}
    for td, h1, h3, h5, reg in rows:
        bl = _bucket_label(temp_for(td), t_tighten, t_loosen)
        buckets.setdefault(bl, []).append((td, h1, h3, h5, reg))
    print("\n## 按封板成功率分桶\n")
    print("| 桶 | 样本(T+1) | T+1命中率 | 是否≥min_sample |\n|---|---|---|---|")
    for bl in ("冷(封板成功率<收紧线)", "中(收紧线~放宽线)", "热(封板成功率≥放宽线)", "无数据"):
        sub = buckets.get(bl, [])
        cnt, r = _rate(sub, 1)
        ok = "是" if cnt >= args.min_sample else "否(样本不足)"
        print(f"| {bl} | {cnt} | {'—' if r is None or cnt < args.min_sample else f'{r:.2f}%'} | {ok} |")

    # 按 regime 分桶
    regb: dict[str, list] = {}
    for td, h1, h3, h5, reg in rows:
        regb.setdefault(reg, []).append((td, h1, h3, h5, reg))
    print("\n## 按情绪周期 regime 分桶\n")
    print("| regime | 样本(T+1) | T+1命中率 | 是否≥min_sample |\n|---|---|---|---|")
    for reg, sub in sorted(regb.items(), key=lambda kv: -len(kv[1])):
        cnt, r = _rate(sub, 1)
        ok = "是" if cnt >= args.min_sample else "否(样本不足)"
        print(f"| {reg} | {cnt} | {'—' if r is None or cnt < args.min_sample else f'{r:.2f}%'} | {ok} |")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
