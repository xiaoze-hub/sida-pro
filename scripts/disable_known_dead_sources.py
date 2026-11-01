#!/usr/bin/env python3
"""把三个"生产实测必失败"的数据源在**已存在的库**里关掉（2026-11-01）。

为什么需要这个脚本：`seed_data_sources()` 对已存在的行只刷新 `supports_batch`/`test_symbols`，
**不会改 `enabled`**（刻意如此：不覆盖运维在设置页做的取舍）。所以改种子默认值只对全新库生效，
生产里那三条 0 成功/13,300 失败的源仍然开着、仍然排在链路前段，每次取数先撞一次失败。

本脚本是一次性的显式治理动作（可重复执行，幂等）：

    同花顺实时行情(quote)          0 成功 / 13,300 失败
    智兔分红(dividend)             4 成功 / 10,597 失败（无 token 时几乎必失败）
    FTShare龙虎榜(dragon_tiger)    0 成功 / 569 失败

用法：
    python scripts/disable_known_dead_sources.py            # 干跑，只报当前状态
    python scripts/disable_known_dead_sources.py --apply
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

sys.path.insert(0, ".")

from src.db.session import SessionLocal  # noqa: E402

TARGETS = (
    ("同花顺实时行情", "quote", "ths"),
    ("智兔分红", "dividend", "zhitu"),
    ("FTShare龙虎榜", "dragon_tiger", "ftshare"),
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        changed = 0
        for name, typ, provider in TARGETS:
            row = db.execute(text(
                "select id, enabled, priority, success_count, error_count from data_sources"
                " where name=:n and type=:t and provider=:p"
            ), {"n": name, "t": typ, "p": provider}).fetchone()
            if row is None:
                print(f"  跳过 {name}: 库里没有这条（新库由 seed 默认关闭）")
                continue
            sid, enabled, prio, ok, err = row
            print(f"  {name}: id={sid} enabled={enabled} prio={prio} ok={ok} err={err}")
            if enabled and args.apply:
                db.execute(text("update data_sources set enabled=false where id=:i"), {"i": sid})
                changed += 1
        if args.apply:
            db.commit()
            print(f"已关闭 {changed} 个源（设置页可随时重新开启）")
        else:
            print("干跑结束（加 --apply 才写库）")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
