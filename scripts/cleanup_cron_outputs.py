#!/usr/bin/env python3
"""cron 报告输出保留期清理(2026-10-11)。

背景: `/api/reports/list` 报告中心 + 首页每 30s 轮询会扫 `CRON_OUTPUT_DIR`
(`~/.hermes/cron/output/<job_id>/*.md`)。该目录此前**无保留期**, .md 无限堆积
→ 扫描量随时间单调增长(生产已 457 文件、30-45s 渲染)。本脚本按 mtime 删除
超过保留期的报告, 控制目录规模。

策略: 默认保留 30 天; 只删目录内的 `*.md`(不碰子目录结构本身 / 非 md 文件);
`--dry-run` 打印将删清单而不落删。

用法:
  python scripts/cleanup_cron_outputs.py                 # 删 30 天前的报告
  python scripts/cleanup_cron_outputs.py --days 60       # 保留 60 天
  python scripts/cleanup_cron_outputs.py --dry-run       # 只预览
  python scripts/cleanup_cron_outputs.py --dir /path/to  # 指定根目录(默认读 CRON_OUTPUT_DIR)

接入 cron(运维可选): 例如每周日凌晨 3 点执行一次
  `0 3 * * 0  cd /app && python scripts/cleanup_cron_outputs.py --days 30`
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _default_dir() -> Path:
    # 先尊重显式 env, 再退回 core 解析(与 web / report_generator 同一处)。
    env = os.environ.get("CRON_OUTPUT_DIR") or os.environ.get("HERMES_HOME")
    if env:
        p = Path(env)
        # HERMES_HOME 指向 <home>, 报告在 <home>/cron/output; CRON_OUTPUT_DIR 直接指根部。
        return p if p.name == "output" else p / "cron" / "output"
    from src.core.paths import CRON_OUTPUT_DIR  # noqa: PLC0415

    return CRON_OUTPUT_DIR


def cleanup(root: Path, days: int, dry_run: bool = False) -> tuple[int, int]:
    """删除 root/*/*.md 中 mtime 早于 cutoff 者。返回 (删除数, 释放字节)。"""
    cutoff = time.time() - days * 86400
    removed = 0
    freed = 0
    if not root.exists():
        print(f"cleanup_cron_outputs: 目录不存在, 跳过: {root}")
        return 0, 0

    for job_dir in sorted(root.iterdir()):
        if not job_dir.is_dir():
            continue
        for f in job_dir.iterdir():
            if not f.is_file() or not f.name.endswith(".md"):
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                continue
            removed += 1
            freed += st.st_size
            if dry_run:
                print(f"  [dry-run] 将删 {f}  ({st.st_size}B)")
            else:
                try:
                    f.unlink()
                    print(f"  删除 {f}")
                except OSError as e:  # noqa: BLE001
                    removed -= 1
                    freed -= st.st_size
                    print(f"  删除失败 {f}: {e}")

    verb = "将删" if dry_run else "已删"
    print(
        f"cleanup_cron_outputs: {verb} {removed} 个报告, "
        f"释放 {freed / 1024:.1f} KiB (保留 {days} 天, 根目录 {root})"
    )
    return removed, freed


def main() -> int:
    ap = argparse.ArgumentParser(description="cron 报告输出保留期清理")
    ap.add_argument("--days", type=int, default=30, help="保留天数(默认 30)")
    ap.add_argument("--dir", default=None, help="报告根目录(默认 CRON_OUTPUT_DIR)")
    ap.add_argument("--dry-run", action="store_true", help="只预览不删除")
    args = ap.parse_args()

    if args.days < 1:
        print("--days 必须 >= 1", file=sys.stderr)
        return 2

    root = Path(args.dir) if args.dir else _default_dir()
    cleanup(root, args.days, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
