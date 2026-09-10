#!/usr/bin/env python3
"""TimescaleDB 存储治理(批次1-A, 2026-09-10)。

背景: PG 装了 TimescaleDB 2.29.2 却**零治理** —— 库 17 GB 里 `l2_ticks` 独占 16 GB
(7564 万行 / 仅 7 天 / ≈2.3 GB/天), 无压缩、无保留策略。本脚本做两件事:

1. **klines**(已是 hypertable; 唯一索引 `(symbol,market,period,ts,source,adjust)` **含分区列 ts**)
   → 开启压缩 + 压缩策略(30 天前的 chunk 自动压缩)。**安全**。
2. **l2_ticks** → **只体检, 不改**。原因(实测): 它的唯一索引
   `uq_l2_ticks_dedupe(symbol,market,source,tick_time,direction,price,vol,amt)` **不含分区列**,
   TimescaleDB 要求唯一索引必须包含分区列 → 直接 `create_hypertable` 会失败。
   且 `ts` 存的是**入库时间**(每批同一个 `now`, 见 `history_store.persist_l2_ticks`),
   不是行情时间 → 简单把 `ts` 加进唯一键会**破坏重拉幂等**(重复入库)。
   正确改法涉及语义变更, 见 docs/数据落库与共享缓存设计_20260910.md §5 附注, 待决策。

用法:
    python scripts/ts_storage_governance.py            # 体检(只读)
    python scripts/ts_storage_governance.py --apply    # 执行 klines 压缩(幂等)
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text


def _rows(session, sql: str, **params):
    try:
        return session.execute(text(sql), params).fetchall()
    except Exception as e:  # noqa: BLE001
        return [("ERR", str(e)[:200])]


def check(session) -> None:
    print("== 体检 ==")
    print("  timescaledb:", _rows(session, "select extversion from pg_extension where extname='timescaledb'"))
    print("  hypertables:", _rows(session, "select hypertable_name from timescaledb_information.hypertables"))
    print("  库大小:", _rows(session, "select pg_size_pretty(pg_database_size(current_database()))"))
    print("  表体积:", _rows(
        session,
        """
        select relname, pg_size_pretty(pg_total_relation_size(relid))
        from pg_stat_user_tables order by pg_total_relation_size(relid) desc limit 5
        """,
    ))
    print("  策略(jobs):", _rows(
        session,
        "select job_id, application_name, hypertable_name from timescaledb_information.jobs order by job_id",
    ))
    print("  klines 索引:", _rows(
        session, "select indexdef from pg_indexes where tablename='klines'"
    ))
    print("  l2_ticks 索引:", _rows(
        session, "select indexdef from pg_indexes where tablename='l2_ticks'"
    ))
    print(
        "  ⚠️ l2_ticks 阻塞: 唯一索引不含分区列 ts(且 ts=入库时间, 非行情时间) → "
        "转 hypertable 前需先定语义(见设计文档 §5 附注)"
    )


def apply_klines(session) -> None:
    print("== 执行: klines 压缩 ==")
    stmts = [
        (
            "开启压缩(segmentby 按标的/周期/源/复权, orderby ts DESC)",
            """
            ALTER TABLE klines SET (
              timescaledb.compress,
              timescaledb.compress_segmentby = 'symbol,market,period,source,adjust',
              timescaledb.compress_orderby = 'ts DESC'
            )
            """,
        ),
        (
            "压缩策略: 30 天前的 chunk 自动压缩",
            "SELECT add_compression_policy('klines', INTERVAL '30 days', if_not_exists => TRUE)",
        ),
    ]
    for desc, sql in stmts:
        try:
            session.execute(text(sql))
            session.commit()
            print(f"  OK  {desc}")
        except Exception as e:  # noqa: BLE001
            session.rollback()
            print(f"  SKIP/ERR  {desc}: {str(e)[:200]}")
    print("  压缩后体积:", _rows(
        session,
        "select pg_size_pretty(pg_total_relation_size('klines'))",
    ))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="执行 klines 压缩(默认只体检)")
    args = ap.parse_args()

    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        check(db)
        if args.apply:
            apply_klines(db)
        else:
            print("\n(只体检; 加 --apply 才执行 klines 压缩)")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
