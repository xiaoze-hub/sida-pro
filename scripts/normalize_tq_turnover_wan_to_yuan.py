#!/usr/bin/env python3
"""把 `quote_snapshots` 里 tq 快照的历史成交额从「万元」统一成「元」（2026-11-01）。

背景：TQ 快照的 `Amount` 字段单位是**万元**，`vendors/tq.py` 已按 `×1e4` 转成元写进
`Quote.turnover`；但 2026-09-21~09-30 那批早期落库是原始值直接入库，导致同表两种单位。

实测（600111 同票同日可复现）：`volume(股) × last ≈ turnover × 1e4`
  - 688008 2026-09-22 09:31：7,526,900 股 × 233.0 = 1.7537e9；turnover=173,534.47 ⇒ ×1e4 = 1.735e9 ✓

**判据**：`volume × last / turnover` 落在 5,000~20,000（即差 1 万倍）。换算后比值回到 ≈1，
条件不再成立 ⇒ 幂等，重复执行不会二次放大。

用法：
    python scripts/normalize_tq_turnover_wan_to_yuan.py           # 干跑
    python scripts/normalize_tq_turnover_wan_to_yuan.py --apply
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

sys.path.insert(0, ".")

from src.db.session import SessionLocal  # noqa: E402

_PREDICATE = """
    market = 'CN' AND source = 'tq'
    AND volume IS NOT NULL AND volume > 0
    AND last IS NOT NULL AND last > 0
    AND turnover IS NOT NULL AND turnover > 0
    AND (volume * last) / turnover BETWEEN 5000 AND 20000
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sample", type=int, default=3)
    args = ap.parse_args()

    db = SessionLocal()
    try:
        n = db.execute(text(f"select count(*) from quote_snapshots where {_PREDICATE}")).scalar() or 0
        print(f"待换算（万元→元）: {n} 行")
        if args.sample and n:
            for r in db.execute(text(
                f"select symbol, ts, volume, last, turnover from quote_snapshots where {_PREDICATE} limit :n"
            ), {"n": args.sample}).fetchall():
                ratio = (r[2] * r[3]) / r[4] if r[4] else 0
                print(f"  样例 {r[0]} {r[1]} vol={r[2]:.0f} px={r[3]} amt={r[4]} (vol*px/amt={ratio:.1f})"
                      f" → amt={r[4] * 1e4:.0f}")
        if not args.apply:
            print("干跑结束（加 --apply 才写库）")
            return 0
        res = db.execute(text(f"update quote_snapshots set turnover = turnover * 1e4 where {_PREDICATE}"))
        db.commit()
        print(f"已更新 {res.rowcount} 行（turnover ×1e4）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
