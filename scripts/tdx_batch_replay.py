# -*- coding: utf-8 -*-
"""批量触发通达信超盘回放生成 .tck 文件。

## 原理

TdxW 的「超盘回放」是 GUI 操作, TQ API 无法直接触发。
本脚本用 Windows UI 自动化(pyautogui)控制 TdxW 客户端:
1. 通过 TQ API `exec_to_tdx` 跳转到目标股票
2. 用键盘快捷键触发「超盘回放」
3. 等待回放完成, .tck 文件自动落盘到 zst_cache/

## 前提

- TdxW 客户端已启动且可见
- TQ 网关可达(127.0.0.1:17709)
- 已安装 pyautogui: pip install pyautogui

## 用法

python scripts/tdx_batch_replay.py --symbols 000001,000002,600519 --date 20260915
python scripts/tdx_batch_replay.py --file symbols.txt --date 20260915
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# TQ 网关
TQ_URL = "http://127.0.0.1:17709/"

# TdxW zst_cache 目录
TDX_DIR = Path(r"C:\new_tdx64")
ZST_CACHE = TDX_DIR / "T0002" / "zst_cache"

# 目标目录(容器可读)
OUTPUT_DIR = Path(r"\\wsl$\docker-desktop-data\data\docker\volumes\panwatch-tck\_data")
# 备用: 直接复制到项目 data/tck
FALLBACK_OUTPUT = Path(__file__).parent.parent / "data" / "tck"


def tq_rpc(method: str, params: dict, timeout: float = 5.0):
    """TQ JSON-RPC 调用。"""
    body = json.dumps({"id": 1, "method": method, "params": params}, ensure_ascii=False).encode("utf-8")
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(TQ_URL, content=body,
                          headers={"Content-Type": "application/json; charset=utf-8"})
        data = json.loads(resp.content.decode("utf-8"))
    return data.get("result", {}).get("Value")


def to_tq_code(symbol: str) -> str:
    """6位代码 → TQ格式(000001.SZ)。"""
    s = symbol.strip()
    if s[0] in ("6", "9") or s.startswith("688"):
        return f"{s}.SH"
    return f"{s}.SZ"


def to_tck_prefix(symbol: str) -> str:
    """6位代码 → .tck文件前缀(sz000001)。"""
    s = symbol.strip()
    if s[0] in ("6", "9") or s.startswith("688"):
        return f"sh{s}"
    return f"sz{s}"


def navigate_to_stock(symbol: str) -> bool:
    """通过 TQ API 跳转到指定股票。"""
    tq_code = to_tq_code(symbol)
    try:
        r = tq_rpc("exec_to_tdx", {"url": f"http://www.treeid/code_{tq_code}"})
        if isinstance(r, dict) and r.get("ErrorId") == "0":
            return True
        logger.warning("跳转失败 %s: %s", symbol, r)
        return False
    except Exception as e:
        logger.error("TQ 调用失败 %s: %s", symbol, e)
        return False


def trigger_replay_via_keyboard():
    """用键盘快捷键触发超盘回放(需 TdxW 窗口聚焦)。

    TdxW 超盘回放快捷键: 无默认快捷键, 需通过菜单:
    分析 → 超盘回放 (或右键菜单)

    这里用 pyautogui 模拟菜单点击, 需要窗口位置固定。
    实际快捷键可能因版本不同, 需要调整。
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True  # 鼠标移到左上角可中断

        # 方法1: 尝试已知快捷键(部分版本支持)
        # pyautogui.hotkey('ctrl', 'r')  # 刷新
        # pyautogui.hotkey('f5')  # 刷新

        # 方法2: 通过菜单(需要窗口位置)
        # 这里只是示例, 实际需要根据 TdxW 窗口位置调整
        logger.info("需要手动触发超盘回放或配置窗口位置")
        return False

    except ImportError:
        logger.error("pyautogui 未安装: pip install pyautogui")
        return False


def wait_for_tck(symbol: str, date: str, timeout: int = 60) -> Path | None:
    """等待 .tck 文件生成。"""
    prefix = to_tck_prefix(symbol)
    tck_name = f"{prefix}_{date}.tck"
    tck_path = ZST_CACHE / tck_name

    start = time.time()
    while time.time() - start < timeout:
        if tck_path.exists():
            logger.info("找到 .tck: %s", tck_path)
            return tck_path
        time.sleep(1)

    logger.warning("超时未找到 %s", tck_name)
    return None


def copy_tck_to_output(tck_path: Path, symbol: str, date: str) -> bool:
    """复制 .tck 到容器可读目录。"""
    prefix = to_tck_prefix(symbol)
    tck_name = f"{prefix}_{date}.tck"

    # 尝试多个目标目录
    for output_dir in [OUTPUT_DIR, FALLBACK_OUTPUT]:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            dest = output_dir / tck_name
            shutil.copy2(tck_path, dest)
            logger.info("复制到: %s", dest)
            return True
        except Exception as e:
            logger.debug("复制到 %s 失败: %s", output_dir, e)

    logger.error("所有目标目录都不可写")
    return False


def process_stock(symbol: str, date: str, wait_timeout: int = 30) -> bool:
    """处理单只股票: 跳转 → 等待 .tck → 复制。"""
    logger.info("处理 %s ...", symbol)

    # 检查是否已有 .tck
    prefix = to_tck_prefix(symbol)
    tck_name = f"{prefix}_{date}.tck"
    existing = ZST_CACHE / tck_name
    if existing.exists():
        logger.info("%s 已有 .tck, 直接复制", symbol)
        return copy_tck_to_output(existing, symbol, date)

    # 跳转到股票
    if not navigate_to_stock(symbol):
        return False

    # 等待 .tck 生成(需要 TdxW 自动或手动触发回放)
    tck_path = wait_for_tck(symbol, date, timeout=wait_timeout)
    if not tck_path:
        return False

    return copy_tck_to_output(tck_path, symbol, date)


def scan_existing_tcks(date: str | None = None) -> list[Path]:
    """扫描已有的 .tck 文件。"""
    if not ZST_CACHE.exists():
        return []

    pattern = f"*_{date}.tck" if date else "*.tck"
    return sorted(ZST_CACHE.glob(pattern))


def copy_all_existing(date: str | None = None) -> int:
    """复制所有已有的 .tck 文件。"""
    tcks = scan_existing_tcks(date)
    if not tcks:
        logger.info("zst_cache 无 .tck 文件")
        return 0

    count = 0
    for tck_path in tcks:
        # 从文件名解析 symbol 和 date
        name = tck_path.stem  # sz000001_20260915
        parts = name.split("_")
        if len(parts) >= 2:
            prefix = parts[0]  # sz000001
            tck_date = parts[1]  # 20260915
            symbol = prefix[2:]  # 000001
            if copy_tck_to_output(tck_path, symbol, tck_date):
                count += 1

    logger.info("复制了 %d 个 .tck 文件", count)
    return count


def main():
    parser = argparse.ArgumentParser(description="批量触发通达信超盘回放")
    parser.add_argument("--symbols", help="股票代码, 逗号分隔")
    parser.add_argument("--file", help="股票代码文件(每行一个)")
    parser.add_argument("--date", help="日期 YYYYMMDD, 默认今天")
    parser.add_argument("--scan-only", action="store_true", help="只扫描复制已有 .tck")
    parser.add_argument("--wait", type=int, default=30, help="等待 .tck 生成的超时秒数")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from datetime import datetime
    date = args.date or datetime.now().strftime("%Y%m%d")

    # 只扫描模式
    if args.scan_only:
        copy_all_existing(date)
        return

    # 收集股票代码
    symbols = []
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    elif args.file:
        with open(args.file) as f:
            symbols = [line.strip() for line in f if line.strip()]

    if not symbols:
        logger.error("请指定 --symbols 或 --file")
        sys.exit(1)

    logger.info("处理 %d 只股票, 日期 %s", len(symbols), date)
    logger.info("注意: 需要 TdxW 窗口可见, 且超盘回放功能可用")
    logger.info("如果 .tck 未自动生成, 请手动在 TdxW 中触发「超盘回放」")

    # 先复制已有
    copy_all_existing(date)

    # 逐只处理
    success = 0
    for symbol in symbols:
        if process_stock(symbol, date, wait_timeout=args.wait):
            success += 1

    logger.info("完成: %d/%d 成功", success, len(symbols))


if __name__ == "__main__":
    main()
