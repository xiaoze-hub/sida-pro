#!/usr/bin/env python3
"""生成真实交易日真相 fixture(离线只读)—— tests/fixtures/real_trading_days.txt。

2026-10-11 交易日历 P1 真相核验配套脚本。

口径
----
生产 PostgreSQL `klines` 表(period='1d')是 A 股**真实日线**的权威落库; 取全市场覆盖
最高的 N 只标的(默认 40, 均含 2025+ 多年日线, 如 600519 贵州茅台)的 distinct 交易日
并集作为"真交易日"真相。并集内: 无周末、无稀疏噪声日(HK 标的 00700 等偶有串入, 当日
仅 1-2 行 → 阈值过滤), 因此可当权威。

用法(只读, 禁写库):
    python3 scripts/gen_real_trading_days_fixture.py            # 默认窗口 2025-01-02~
    python3 scripts/gen_real_trading_days_fixture.py --top 40 --lo 2025-01-02 --hi 2026-08-25

连接: 复用项目 PG 环境变量; 若在宿主机上无 PG 密码, 可用:
    sudo -u postgres psql -d sida -tAc "<同下 SQL>"
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SQL = """
with tops as (
  select symbol from klines where period='1d'
  group by symbol order by count(*) desc limit {top}
)
select (k.ts at time zone 'Asia/Shanghai')::date as dt,
       count(distinct k.symbol) as n
from klines k join tops t on k.symbol = t.symbol
where k.period='1d' and k.ts >= '{lo}'
group by 1
having count(distinct k.symbol) >= {min_sym}
order by 1
"""


def _run_psql(sql: str) -> list[str]:
    db_url = os.environ.get("SIDA_DB_URL") or os.environ.get("DATABASE_URL_WRITE") or ""
    if db_url.startswith("postgresql"):
        # 走 psql 连接串
        out = subprocess.run(
            ["psql", db_url, "-tA", "-c", sql], capture_output=True, text=True, check=True
        ).stdout
    else:
        # 退化: 宿主机以 postgres 超级用户只读查询
        out = subprocess.run(
            ["sudo", "-n", "-u", "postgres", "psql", "-d", "sida", "-tAc", sql],
            capture_output=True, text=True, check=True,
        ).stdout
    return [ln.split("|")[0].strip() for ln in out.splitlines() if ln.strip()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--min-sym", type=int, default=5, dest="min_sym")
    ap.add_argument("--lo", default="2025-01-02")
    ap.add_argument("--hi", default="")  # 默认取全量
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1]
                                         / "tests" / "fixtures" / "real_trading_days.txt"))
    args = ap.parse_args()

    rows = _run_psql(SQL.format(top=args.top, lo=args.lo, min_sym=args.min_sym))
    dates = [d for d in rows if (not args.hi or d <= args.hi)]
    if not dates:
        print("未取到任何日期 —— 检查 PG 连接/窗口", file=sys.stderr)
        return 1
    print(f"{len(dates)} 个交易日 → {args.out}")
    Path(args.out).write_text("\n".join(dates) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
