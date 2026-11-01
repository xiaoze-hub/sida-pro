#!/usr/bin/env python3
"""把 `quote_snapshots` 里 tq 快照的历史成交额从「万元」统一成「元」（2026-11-01）。

背景：TQ 快照的 `Amount` 字段单位是**万元**，`vendors/tq.py` 已按 `×1e4` 转成元写进
`Quote.turnover`；但 2026-09-21~09-30 那批早期落库是原始值直接入库，导致同表两种单位。

**为什么判据要分两档**（审查指出）：这两批行同时存在两个单位问题，取决于执行顺序：
  - 若 `volume` 仍是**手**（尚未跑 volume 迁移）：`volume×last/turnover` ≈ **100**
      （手比股小 100，万元比元大 1e4 ⇒ 净差 100）
  - 若 `volume` 已是**股**（已跑 `normalize_quote_volume_lots_to_shares.py`）：比值 ≈ **10000**
两档都要能识别并一次换算到位（手→股、万元→元），否则换库重跑会静默 0 行。

换算后比值回到 ≈1，两档条件都不再成立 ⇒ 幂等，重复执行不会二次放大。

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

_BASE = """
    market = 'CN' AND source = 'tq'
    AND volume IS NOT NULL AND volume > 0
    AND last IS NOT NULL AND last > 0
    AND turnover IS NOT NULL AND turnover > 0
"""

# 档 A（volume 仍是手）: 比值 ≈100 → 两个字段都要换
_LOTS_BAND = f"{_BASE} AND (volume * last) / turnover BETWEEN 80 AND 130"
# 档 B（volume 已是股）: 比值 ≈1e4 → 只换成交额
_SHARES_BAND = f"{_BASE} AND (volume * last) / turnover BETWEEN 5000 AND 20000"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sample", type=int, default=2)
    args = ap.parse_args()

    db = SessionLocal()
    try:
        n_lots = db.execute(text(f"select count(*) from quote_snapshots where {_LOTS_BAND}")).scalar() or 0
        n_shares = db.execute(text(f"select count(*) from quote_snapshots where {_SHARES_BAND}")).scalar() or 0
        print(f"档A（volume=手, turnover=万元，需两者都换）: {n_lots} 行")
        print(f"档B（volume=股, turnover=万元，只换成交额）: {n_shares} 行")

        for label, pred in (("A", _LOTS_BAND), ("B", _SHARES_BAND)):
            if args.sample:
                for r in db.execute(text(
                    f"select symbol, ts, volume, last, turnover from quote_snapshots where {pred} limit :n"
                ), {"n": args.sample}).fetchall():
                    ratio = (r[2] * r[3]) / r[4] if r[4] else 0
                    print(f"  样例[{label}] {r[0]} {r[1]} vol={r[2]:.0f} px={r[3]} amt={r[4]}"
                          f" (比值 {ratio:.0f})")

        if not args.apply:
            print("干跑结束（加 --apply 才写库）")
            return 0

        r1 = db.execute(text(
            f"update quote_snapshots set volume = volume * 100, turnover = turnover * 1e4 where {_LOTS_BAND}"
        ))
        r2 = db.execute(text(
            f"update quote_snapshots set turnover = turnover * 1e4 where {_SHARES_BAND}"
        ))
        db.commit()
        print(f"已更新: 档A {r1.rowcount} 行（volume×100 & turnover×1e4）/ 档B {r2.rowcount} 行（turnover×1e4）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
