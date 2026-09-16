# -*- coding: utf-8 -*-
"""通达信超盘回放 - 交互式向导。

一步步引导用户完成超盘回放, 自动检测 .tck 生成并复制。

用法:
    python scripts/tdx_replay_wizard.py
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path

import pygetwindow as gw

ZST_CACHE = Path(r"C:\new_tdx64\T0002\zst_cache")
OUTPUT_DIR = Path(r"C:\Users\tianxiang\sida-work\data\tck")


def find_window():
    for w in gw.getWindowsWithTitle("通达信"):
        if w.visible:
            return w
    return None


def main():
    print("=" * 60)
    print("  通达信超盘回放向导")
    print("=" * 60)
    print()

    # 检查窗口
    win = find_window()
    if not win:
        print("❌ 未找到通达信窗口!")
        print("   请先启动 TdxW 客户端, 然后重新运行本脚本")
        return
    print(f"✅ 找到窗口: {win.title}")
    print(f"   位置: ({win.left}, {win.top}) 大小: {win.width}x{win.height}")
    print()

    # 输入股票代码
    symbol = input("请输入股票代码 (如 000001): ").strip()
    if not symbol:
        print("未输入代码, 退出")
        return

    # 判断市场前缀
    if symbol[0] in ("6", "9") or symbol.startswith("688"):
        prefix = f"sh{symbol}"
    else:
        prefix = f"sz{symbol}"

    date = input(f"请输入日期 YYYYMMDD (默认今天 {datetime.now().strftime('%Y%m%d')}): ").strip()
    if not date:
        date = datetime.now().strftime("%Y%m%d")

    tck_name = f"{prefix}_{date}.tck"
    tck_path = ZST_CACHE / tck_name

    print()
    print(f"目标文件: {tck_name}")
    print()

    # 检查是否已有
    if tck_path.exists():
        size = tck_path.stat().st_size
        print(f"✅ 文件已存在 ({size:,} bytes)")
        if input("是否复制到项目目录? (y/n): ").strip().lower() == "y":
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            dest = OUTPUT_DIR / tck_name
            shutil.copy2(tck_path, dest)
            print(f"✅ 已复制到: {dest}")
        return

    # 引导用户操作
    print("请按以下步骤操作:")
    print()
    print("  1️⃣  在通达信中输入代码切换到目标股票:")
    print(f"      输入 '{symbol}' 然后按 Enter")
    print()
    print("  2️⃣  打开超盘回放:")
    print("      菜单栏 → 分析 → 超盘回放")
    print("      (或右键K线图 → 超盘回放)")
    print()
    print("  3️⃣  在回放对话框中:")
    print(f"      选择日期: {date}")
    print("      点击「开始」或「回放」")
    print()
    print("  4️⃣  等待回放完成 (进度条走完)")
    print()
    input("完成上述步骤后按 Enter 继续...")
    print()

    # 等待文件生成
    print(f"等待 {tck_name} 生成...")
    timeout = 120
    start = time.time()
    while time.time() - start < timeout:
        if tck_path.exists():
            size = tck_path.stat().st_size
            elapsed = int(time.time() - start)
            print(f"✅ 文件已生成! ({size:,} bytes, 耗时 {elapsed}s)")
            break
        elapsed = int(time.time() - start)
        print(f"   等待中... ({elapsed}s/{timeout}s)", end="\r")
        time.sleep(2)
    else:
        print()
        print(f"❌ 超时未找到 {tck_name}")
        print("   请确认回放已完成, 然后手动运行:")
        print(f"   python scripts/tdx_copy_tcks.py")
        return

    # 复制
    print()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUTPUT_DIR / tck_name
    shutil.copy2(tck_path, dest)
    print(f"✅ 已复制到: {dest}")
    print()

    # 验证
    if dest.exists():
        size = dest.stat().st_size
        print(f"验证: {dest.name} ({size:,} bytes)")
        print()
        print("下一步: 在容器中测试解析")
        print(f'  docker exec panwatch python -c "')
        print(f'  from src.core.tdx_tick_parser import parse_tck')
        print(f'  trades, orders, cancels = parse_tck(\'/app/data/tck/{tck_name}\')')
        print(f'  print(f\'成交 {{len(trades)}} 笔, 委托 {{len(orders)}} 笔, 撤单 {{len(cancels)}} 笔\')')
        print(f'  "')
    print()
    print("完成!")


if __name__ == "__main__":
    main()
