#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决策账本 `t_n_span` 存量一次性回填(2026-10-15 停牌跨期口径标注)。

背景(生产实证): 决策账本的 T+n 取「信号日起**第 n 根可用 K 线**」, 遇长期停牌会把一个月后的
收盘当 T+1 —— symbol=300082 信号日 20260918, 下一根可用 K 线是 2026-10-13(相隔 11 个交易日),
`ret_t1=-0.0886` 实为一月后跌幅。用户裁决: **不重算存量数值, 只标注 + 排除统计**。

本脚本给 **v198 之前已填的行**(有 ret_t1/3/5 但缺 `t_n_span`)补跨度列:
  - 用与 `backfill_outcomes` 同一个 K 线序列口径重取 T+1 取值日, 算交易日跨度写入 `t_n_span`;
  - 跨度 > 5 → `outcome_state='suspended_gap'`(覆盖原状态), 收益列**保持原值不动**;
  - 幂等: 只碰 `t_n_span IS NULL` 的行, 重跑对已填行无副作用(可安全重跑)。

**只写 `t_n_span` / `outcome_state` / `outcome_note` 三列, 绝不改写 ret_*/hit_*** ——
生产跑前请确认 `t_n_span` 列已由 v198 迁移建好(应用启动即自动迁移)。

用法:
  # 生产 PG(只读排查可先 --limit 小值试跑)
  SIDA_DB_URL="postgresql+psycopg2://user:pw@host:5432/sida" \\
    python scripts/backfill_decision_spans.py --limit 20000
  # 本地 sqlite
  python scripts/backfill_decision_spans.py --db sqlite:///data/sida.db
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _dsn(args) -> str:
    if args.db:
        return args.db
    for env in ("DATABASE_URL_READ", "SIDA_DB_URL", "DATABASE_URL_WRITE"):
        v = os.environ.get(env)
        if v:
            return v
    return "sqlite:///data/sida.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help="SQLAlchemy DSN(缺省读环境变量/本地 sqlite)")
    ap.add_argument("--limit", type=int, default=20000, help="单跑最多处理的行数")
    args = ap.parse_args()

    from sqlalchemy import create_engine

    from src.core.decision_log import backfill_spans

    eng = create_engine(_dsn(args))
    out = backfill_spans(eng, limit=args.limit)
    print(json.dumps(out, ensure_ascii=False))
    if out.get("skipped_no_column"):
        print("⚠️  decision_log 无 t_n_span 列(未跑 v198 迁移) —— 先启动应用跑迁移再回填。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
