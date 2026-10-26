#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 `.img` 校准 fixture(真实样本 → 截断窗口 → 重新封装容器)。

背景
----
`src/core/tdx_img_parser.py` 的格式校准基于真实样本 `sz002361_20260827/28.img`
(365KB+, 位于开发机 `~/tdx_data/`, 不入库)。为了让**离线测试**可复现,
本脚本从真实样本里截取一段**连续帧窗口**, 逐帧按原字节重封进一个**合法容器**
(24B 头 + zlib), 产出仓库内的小 fixture(`tests/fixtures/*.img`)。

帧字节是**真实数据**, 只做窗口截断 + 重新压缩(不改任何字段) —— 因此校验的是
真实字节结构, 不是合成样本。头里的 magic 沿用源文件(容器校验不依赖 magic)。

用法::

    python scripts/gen_img_fixture.py \
        --src ~/tdx_data/sz002361_20260827.img \
        --out tests/fixtures/tdx_img_sz002361_open.img \
        --start 0 --count 120
"""
from __future__ import annotations

import argparse
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.tdx_img_parser import decode_container, iter_records  # noqa: E402


def build(src: Path, out: Path, start: int, count: int) -> dict:
    """截取 [start, start+count) 帧, 用源文件 magic 重封装为 .img。"""
    data = src.read_bytes()
    raw = decode_container(data)
    frames = list(iter_records(raw))
    window = frames[start:start + count]
    if not window:
        raise SystemExit(f"窗口为空: start={start} count={count} 总帧={len(frames)}")
    body = b"".join(b"\x03" + fr + b"\x04" for fr in window)
    comp = zlib.compress(body)
    magic = data[:4]
    header = magic + b"\x00\x00\x00\x00" + len(comp).to_bytes(8, "little") + len(body).to_bytes(8, "little")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(header + comp)
    return {
        "src": str(src), "out": str(out), "magic": magic.hex(),
        "frames_total": len(frames), "frames_used": len(window),
        "bytes": out.stat().st_size, "uncompressed": len(body),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int, default=120)
    args = ap.parse_args()
    info = build(args.src, args.out, args.start, args.count)
    print(" ".join(f"{k}={v}" for k, v in info.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
