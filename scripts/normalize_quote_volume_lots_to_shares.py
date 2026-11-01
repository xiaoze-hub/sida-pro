#!/usr/bin/env python3
"""把 `quote_snapshots.volume` 的历史数据从「手」统一成「股」（2026-11-01）。

背景：腾讯/东财行情接口的成交量字段是**手**，而本项目口径是**股**
（AGENTS.md：`vol × price == amt` 必须对得上）。实测 600111 2026-10-30：

    klines.volume        = 30,179,844 股
    quote_snapshots.volume =    301,798 手   ← 正好 1/100

厂商层已在 2026-11-01 改为落库即股；这张表的历史行仍是手，若不迁移，
同一列会同时存在两种单位（新数据 100 倍于旧数据）。

**只改能自证是手的行**：`volume × 100 × last` 与 `turnover` 相差 ≤5%。
换算后该条件不再成立（会差 100 倍），所以脚本天然幂等 —— 重复执行不会二次放大。

用法：
    python scripts/normalize_quote_volume_lots_to_shares.py            # 干跑，只报数
    python scripts/normalize_quote_volume_lots_to_shares.py --apply    # 真正写库
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

sys.path.insert(0, ".")

from src.db.session import SessionLocal  # noqa: E402

_PREDICATE = """
    market = 'CN'
    AND volume IS NOT NULL AND volume > 0
    AND last IS NOT NULL AND last > 0
    AND turnover IS NOT NULL AND turnover > 0
    AND abs(volume * 100 * last - turnover) <= turnover * 0.05
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只干跑）")
    ap.add_argument("--sample", type=int, default=5, help="干跑时打印几条样例")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        total = db.execute(text("select count(*) from quote_snapshots where market='CN'")).scalar() or 0
        provable = db.execute(text(f"select count(*) from quote_snapshots where {_PREDICATE}")).scalar() or 0
        unprovable = db.execute(text(
            "select count(*) from quote_snapshots where market='CN' and volume is not null"
            f" and not ({_PREDICATE})"
        )).scalar() or 0
        print(f"CN 行数 {total}；可自证为「手」{provable}；其余(无法自证/已是股) {unprovable}")

        if args.sample and provable:
            for r in db.execute(text(
                f"select symbol, ts, volume, last, turnover from quote_snapshots where {_PREDICATE} limit :n"
            ), {"n": args.sample}).fetchall():
                print(f"  样例 {r[0]} {r[1]} vol={r[2]} px={r[3]} amt={r[4]} "
                      f"→ 换算后 vol={r[2] * 100}")

        if not args.apply:
            print("干跑结束（加 --apply 才写库）")
            return 0

        res = db.execute(text(f"update quote_snapshots set volume = volume * 100 where {_PREDICATE}"))
        db.commit()
        print(f"已更新 {res.rowcount} 行（volume ×100）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
