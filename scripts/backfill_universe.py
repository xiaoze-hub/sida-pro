#!/usr/bin/env python3
"""回填 PIT 股票池快照(B0.6/KI-036)。

用法(在仓库根目录执行):
  python scripts/backfill_universe.py --from-candidates          # 用 EntryCandidate 历史快照回填
  python scripts/backfill_universe.py --from-candidates --market CN
  python scripts/backfill_universe.py --today                    # 用当前 Stock 名单生成今天的池
"""

from __future__ import annotations

import argparse
import json
import sys

sys.path.insert(0, ".")


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 stock_universe_snapshots")
    parser.add_argument("--from-candidates", action="store_true", help="用候选历史快照回填(推荐)")
    parser.add_argument("--today", action="store_true", help="用当前 Stock 名单生成今天的池")
    parser.add_argument("--market", default=None, help="市场过滤: CN/HK/US")
    args = parser.parse_args()

    if args.from_candidates:
        from src.core.universe import backfill_from_entry_candidates

        print(json.dumps(backfill_from_entry_candidates(market=args.market), ensure_ascii=False))
        return 0
    if args.today:
        from src.core.universe import backfill_from_stock_table

        print(json.dumps({"rows": backfill_from_stock_table()}, ensure_ascii=False))
        return 0
    parser.error("请指定 --from-candidates 或 --today")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
