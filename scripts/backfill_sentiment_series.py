#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场情绪序列断档补齐(2026-10-11 断链修复): 一次性把 market_sentiment_daily 的
历史缺口(如 2026-09-23 至今)用 TQ `get_scjy_value` 批量补上。

背景: 表曾停在 2026-09-22 无人知(每日同步静默失败 + 无表新鲜度自检)。补数走
`src.core.sentiment_freshness.backfill_missing`(批量 + 重试/降级诚实, 幂等 upsert),
与每日 16:20 的自检自愈**同一实现**。

用法:
  # 本地 sqlite
  python scripts/backfill_sentiment_series.py --db sqlite:///data/sida.db
  # 生产 PG(需写权限; 会真写库)
  SIDA_DB_URL="postgresql+psycopg2://user:pw@host:5432/sida" \\
    python scripts/backfill_sentiment_series.py
  # 只看缺口不补(纯读)
  python scripts/backfill_sentiment_series.py --check-only

输出: 补数前/后的 MAX(date) + 尝试次数; TQ 不可用时**显式报错**(exit 2), 不静默。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _dsn(args) -> str:
    if args.db:
        return args.db
    for env in ("SIDA_DB_URL", "DATABASE_URL_WRITE"):
        v = os.environ.get(env)
        if v:
            return v
    return "sqlite:///data/sida.db"


def main() -> int:
    ap = argparse.ArgumentParser(description="补齐 market_sentiment_daily 历史断档")
    ap.add_argument("--db", default="", help="DSN; 缺省读 SIDA_DB_URL/DATABASE_URL_WRITE 或本地 sqlite")
    ap.add_argument("--check-only", action="store_true", help="只自检不补数(纯读)")
    args = ap.parse_args()

    from sqlalchemy import create_engine

    from src.core import sentiment_freshness as sf

    engine = create_engine(_dsn(args))
    conn = engine.connect()
    try:
        before = sf.latest_sentiment_date(conn)
        chk = sf.check(latest_fn=lambda: before)
        print(f"[自检] 最新 {before} / 期望 {chk['expected']} / 缺 {len(chk['missing'])} 日")
        if chk["missing"]:
            print(f"[缺口] {chk['missing'][0]} … {chk['missing'][-1]}")
        if chk["ok"]:
            print("[OK] 序列已是最新, 无缺口")
            return 0
        if args.check_only:
            print("[check-only] 未补数(有缺口)")
            return 1
        res = sf.backfill_missing(chk["missing"], db=conn)
        print(f"[补数] ok={res['ok']} rows={res['rows']} attempts={res['attempts']} err={res['error']}")
        after = sf.latest_sentiment_date(conn)
        print(f"[结果] MAX(date) {before} → {after}")
        if not res["ok"]:
            print("[FAIL] TQ 补数未成功(显式失败, 未静默)", file=sys.stderr)
            return 2
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
