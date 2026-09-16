# -*- coding: utf-8 -*-
"""通达信超盘回放 UI 自动化脚本。

用 pyautogui 控制 TdxW 客户端, 自动触发「超盘回放」生成 .tck 文件。

## 前提
- TdxW 客户端已启动且窗口可见
- pip install pyautogui pygetwindow

## 用法
    # 对当前打开的股票触发回放
    python scripts/tdx_auto_replay.py

    # 指定股票和日期
    python scripts/tdx_auto_replay.py --symbol 000001 --date 20260915

    # 批量处理
    python scripts/tdx_auto_replay.py --symbols 000001,000002,600519

## 菜单路径
通达信 V7.x: 分析 → 超盘回放
或: 右键K线 → 超盘回放
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from pathlib import Path

import pyautogui
import pygetwindow as gw

logger = logging.getLogger(__name__)

# 安全设置
pyautogui.FAILSAFE = True  # 鼠标移到左上角可中断
pyautogui.PAUSE = 0.3  # 每个操作间暂停

# TdxW zst_cache
ZST_CACHE = Path(r"C:\new_tdx64\T0002\zst_cache")
# 输出目录
OUTPUT_DIR = Path(r"C:\Users\tianxiang\sida-work\data\tck")


def find_tdx_window():
    """查找通达信窗口。"""
    for w in gw.getWindowsWithTitle("通达信"):
        if w.visible:
            return w
    # 尝试其他关键词
    for w in gw.getWindowsWithTitle("TdxW"):
        if w.visible:
            return w
    return None


def activate_window(window) -> bool:
    """激活窗口到前台。"""
    try:
        if window.isMinimized:
            window.restore()
        window.activate()
        time.sleep(0.5)
        return True
    except Exception as e:
        logger.warning("激活窗口失败: %s", e)
        return False


def navigate_to_stock(symbol: str) -> bool:
    """在 TdxW 中输入股票代码跳转。"""
    try:
        # 点击窗口中央确保焦点
        win = find_tdx_window()
        if not win:
            logger.error("未找到通达信窗口")
            return False

        activate_window(win)

        # 方法1: 直接输入代码(通达信支持直接键入代码)
        pyautogui.typewrite(symbol, interval=0.05)
        time.sleep(0.3)
        pyautogui.press("enter")
        time.sleep(1.0)

        logger.info("已跳转到 %s", symbol)
        return True

    except Exception as e:
        logger.error("跳转失败: %s", e)
        return False


def open_replay_via_menu() -> bool:
    """通过菜单打开超盘回放。

    通达信 V7.x 菜单路径: 分析 → 超盘回放
    """
    try:
        win = find_tdx_window()
        if not win:
            return False

        activate_window(win)

        # 点击「分析」菜单(通常在顶部菜单栏)
        # 菜单位置需要根据实际窗口调整
        # 通达信菜单栏通常在窗口顶部
        menu_y = win.top + 50  # 菜单栏高度
        menu_x_analyze = win.left + 200  # 「分析」菜单位置(需调整)

        logger.info("点击「分析」菜单...")
        pyautogui.click(menu_x_analyze, menu_y)
        time.sleep(0.5)

        # 在下拉菜单中找「超盘回放」
        # 需要根据实际菜单位置调整
        replay_y = menu_y + 200  # 超盘回放位置(需调整)
        pyautogui.click(menu_x_analyze, replay_y)
        time.sleep(0.5)

        logger.info("已点击「超盘回放」")
        return True

    except Exception as e:
        logger.error("菜单操作失败: %s", e)
        return False


def open_replay_via_shortcut() -> bool:
    """通过快捷键打开超盘回放。

    通达信超盘回放无默认快捷键, 尝试已知组合。
    """
    try:
        win = find_tdx_window()
        if not win:
            return False

        activate_window(win)

        # 尝试各种快捷键组合
        shortcuts = [
            ("ctrl", "r"),
            ("f8"),
            ("ctrl", "f8"),
            ("alt", "r"),
        ]

        for keys in shortcuts:
            logger.info("尝试快捷键: %s", keys)
            pyautogui.hotkey(*keys)
            time.sleep(1.0)

        return True

    except Exception as e:
        logger.error("快捷键失败: %s", e)
        return False


def trigger_replay_interactive():
    """交互式触发回放 - 显示提示让用户手动操作。"""
    print()
    print("=" * 50)
    print("请在通达信客户端中手动触发「超盘回放」:")
    print()
    print("  1. 确保已切换到目标股票")
    print("  2. 菜单 → 分析 → 超盘回放")
    print("  3. 选择日期, 点击开始")
    print("  4. 等待回放完成")
    print()
    print("完成后按 Enter 继续...")
    print("=" * 50)
    input()


def wait_for_tck(symbol: str, date: str, timeout: int = 120) -> Path | None:
    """等待 .tck 文件生成。"""
    prefix = _to_tck_prefix(symbol)
    tck_name = f"{prefix}_{date}.tck"
    tck_path = ZST_CACHE / tck_name

    logger.info("等待 %s 生成 (超时 %ds)...", tck_name, timeout)
    start = time.time()
    while time.time() - start < timeout:
        if tck_path.exists():
            size = tck_path.stat().st_size
            logger.info("找到 .tck: %s (%d bytes)", tck_name, size)
            return tck_path
        time.sleep(2)

    logger.warning("超时未找到 %s", tck_name)
    return None


def copy_tck(tck_path: Path, symbol: str, date: str) -> bool:
    """复制 .tck 到输出目录。"""
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        dest = OUTPUT_DIR / tck_path.name
        shutil.copy2(tck_path, dest)
        logger.info("复制到: %s", dest)
        return True
    except Exception as e:
        logger.error("复制失败: %s", e)
        return False


def _to_tck_prefix(symbol: str) -> str:
    """6位代码 → .tck前缀(sz000001)。"""
    s = symbol.strip()
    if s[0] in ("6", "9") or s.startswith("688"):
        return f"sh{s}"
    return f"sz{s}"


def process_stock(symbol: str, date: str, interactive: bool = False) -> bool:
    """处理单只股票。"""
    logger.info("处理 %s (%s)", symbol, date)

    # 检查是否已有 .tck
    prefix = _to_tck_prefix(symbol)
    tck_name = f"{prefix}_{date}.tck"
    existing = ZST_CACHE / tck_name
    if existing.exists():
        logger.info("%s 已有 .tck", symbol)
        return copy_tck(existing, symbol, date)

    # 跳转到股票
    if not navigate_to_stock(symbol):
        return False

    # 触发回放
    if interactive:
        trigger_replay_interactive()
    else:
        # 尝试自动触发
        if not open_replay_via_menu():
            logger.warning("自动触发失败, 切换到交互模式")
            trigger_replay_interactive()

    # 等待 .tck
    tck_path = wait_for_tck(symbol, date, timeout=60)
    if not tck_path:
        return False

    return copy_tck(tck_path, symbol, date)


def scan_and_copy_all(date: str | None = None) -> int:
    """扫描并复制所有已有 .tck。"""
    if not ZST_CACHE.exists():
        logger.warning("zst_cache 不存在: %s", ZST_CACHE)
        return 0

    pattern = f"*_{date}.tck" if date else "*.tck"
    tcks = sorted(ZST_CACHE.glob(pattern))

    if not tcks:
        logger.info("无 .tck 文件")
        return 0

    count = 0
    for tck in tcks:
        parts = tck.stem.split("_")
        if len(parts) >= 2:
            symbol = parts[0][2:]
            tck_date = parts[1]
            if copy_tck(tck, symbol, tck_date):
                count += 1

    return count


def main():
    parser = argparse.ArgumentParser(description="通达信超盘回放自动化")
    parser.add_argument("--symbol", help="单只股票代码")
    parser.add_argument("--symbols", help="多只股票, 逗号分隔")
    parser.add_argument("--date", help="日期 YYYYMMDD, 默认今天")
    parser.add_argument("--interactive", action="store_true", help="交互模式(手动触发回放)")
    parser.add_argument("--scan-only", action="store_true", help="只扫描复制已有 .tck")
    parser.add_argument("--list", action="store_true", help="列出已有 .tck")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from datetime import datetime
    date = args.date or datetime.now().strftime("%Y%m%d")

    # 列出模式
    if args.list:
        if ZST_CACHE.exists():
            tcks = sorted(ZST_CACHE.glob("*.tck"))
            print(f"zst_cache 中有 {len(tcks)} 个 .tck 文件:")
            for t in tcks:
                size = t.stat().st_size
                print(f"  {t.name}  ({size:,} bytes)")
        else:
            print(f"zst_cache 不存在: {ZST_CACHE}")
        return

    # 只扫描模式
    if args.scan_only:
        count = scan_and_copy_all(date)
        print(f"复制了 {count} 个 .tck 文件")
        return

    # 收集股票
    symbols = []
    if args.symbol:
        symbols = [args.symbol]
    elif args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    if not symbols:
        # 默认: 只扫描复制
        print("未指定股票, 扫描已有 .tck...")
        count = scan_and_copy_all(date)
        print(f"复制了 {count} 个 .tck 文件")
        return

    # 检查窗口
    win = find_tdx_window()
    if not win:
        logger.error("未找到通达信窗口, 请先启动 TdxW")
        sys.exit(1)

    logger.info("找到窗口: %s", win.title)
    logger.info("处理 %d 只股票, 日期 %s", len(symbols), date)

    # 先复制已有
    scan_and_copy_all(date)

    # 逐只处理
    success = 0
    for symbol in symbols:
        if process_stock(symbol, date, interactive=args.interactive):
            success += 1

    logger.info("完成: %d/%d 成功", success, len(symbols))


if __name__ == "__main__":
    main()
