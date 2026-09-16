# -*- coding: utf-8 -*-
"""扫描并复制已有 .tck 文件到容器可读目录。

用法:
    python scripts/tdx_copy_tcks.py                    # 复制所有
    python scripts/tdx_copy_tcks.py --date 20260915    # 只复制指定日期
    python scripts/tdx_copy_tcks.py --list             # 只列出不复制
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# TdxW zst_cache
ZST_CACHE = Path(r"C:\new_tdx64\T0002\zst_cache")

# 输出目录(按优先级)
OUTPUT_DIRS = [
    Path(r"C:\Users\tianxiang\sida-work\data\tck"),
]


def scan_tcks(date: str | None = None) -> list[Path]:
    """扫描 .tck 文件。"""
    if not ZST_CACHE.exists():
        logger.warning("zst_cache 不存在: %s", ZST_CACHE)
        return []

    pattern = f"*_{date}.tck" if date else "*.tck"
    return sorted(ZST_CACHE.glob(pattern))


def parse_tck_name(path: Path) -> tuple[str, str] | None:
    """从文件名解析 (symbol, date)。sz000001_20260915.tck → (000001, 20260915)"""
    parts = path.stem.split("_")
    if len(parts) >= 2:
        prefix = parts[0]
        date = parts[1]
        if len(prefix) >= 8:
            return prefix[2:], date
    return None


def copy_tck(src: Path, symbol: str, date: str) -> bool:
    """复制 .tck 到输出目录。"""
    filename = src.name
    for output_dir in OUTPUT_DIRS:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            dest = output_dir / filename
            shutil.copy2(src, dest)
            logger.info("复制: %s → %s", filename, dest)
            return True
        except Exception as e:
            logger.debug("复制到 %s 失败: %s", output_dir, e)
    return False


def main():
    parser = argparse.ArgumentParser(description="复制 .tck 文件")
    parser.add_argument("--date", help="日期 YYYYMMDD")
    parser.add_argument("--list", action="store_true", help="只列出不复制")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    tcks = scan_tcks(args.date)
    if not tcks:
        print(f"zst_cache 无 .tck 文件 (目录: {ZST_CACHE})")
        return

    print(f"找到 {len(tcks)} 个 .tck 文件:")
    for tck in tcks:
        info = parse_tck_name(tck)
        size = tck.stat().st_size
        if info:
            symbol, date = info
            print(f"  {tck.name}  ({symbol}, {date}, {size:,} bytes)")
        else:
            print(f"  {tck.name}  ({size:,} bytes)")

    if args.list:
        return

    print()
    success = 0
    for tck in tcks:
        info = parse_tck_name(tck)
        if info:
            symbol, date = info
            if copy_tck(tck, symbol, date):
                success += 1

    print(f"\n复制完成: {success}/{len(tcks)}")
    print(f"输出目录: {OUTPUT_DIRS[0]}")


if __name__ == "__main__":
    main()
