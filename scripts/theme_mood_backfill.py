#!/usr/bin/env python3
"""题材情绪分回填(2026-09-12): 用同一份日线窗口一次算多日。

用法: python scripts/theme_mood_backfill.py --days 40
约束: 需可访问通达信网关(容器内运行); 幂等, 可重复跑。
"""
from __future__ import annotations

import argparse
import json
import sys

from src.core import theme_mood


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=40, help="回填最近 N 个交易日(≤55)")
    args = ap.parse_args()
    days = max(1, min(int(args.days), 55))
    out = theme_mood.scan(write_days=days)
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
