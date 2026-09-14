#!/usr/bin/env python3
"""KI-057: 重算 klines.amplitude 为 A 股通行口径 (high-low)/prev_close*100。

旧口径 (high-low)/low 与带1 实时振幅不一致。历史行混口径比统一更糟。
用法(容器内或连 PG 的环境):
  python scripts/backfill_amplitude_prev_close.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="0=全部")
    args = ap.parse_args()

    sys.path.insert(0, "/app" if __import__("os").path.isdir("/app/src") else ".")
    from sqlalchemy import text

    from src.db.session import engine

    sql = """
    WITH base AS (
      SELECT symbol, ts, high, low, close,
             LAG(close) OVER (PARTITION BY symbol ORDER BY ts) AS prev_close
      FROM klines
      WHERE period = '1d'
    )
    SELECT symbol, ts, high, low, prev_close, amplitude
    FROM base
    WHERE high IS NOT NULL AND low IS NOT NULL AND prev_close IS NOT NULL
      AND prev_close > 0 AND high > 0 AND low > 0
    """
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"

    updated = 0
    skipped = 0
    with engine.begin() as conn:
        rows = conn.execute(text(sql)).fetchall()
        print(f"candidates={len(rows)}")
        for r in rows:
            symbol, ts, high, low, prev_close, old_amp = r
            new_amp = round((float(high) - float(low)) / float(prev_close) * 100, 4)
            if old_amp is not None and abs(float(old_amp) - new_amp) < 0.001:
                skipped += 1
                continue
            if args.dry_run:
                updated += 1
                continue
            conn.execute(
                text(
                    "UPDATE klines SET amplitude = :a "
                    "WHERE symbol = :s AND ts = :ts AND period = '1d'"
                ),
                {"a": new_amp, "s": symbol, "ts": ts},
            )
            updated += 1
    print(f"{'would_update' if args.dry_run else 'updated'}={updated} unchanged={skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
