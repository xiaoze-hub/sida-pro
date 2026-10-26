# -*- coding: utf-8 -*-
"""TdxW TQ 网关 JSON-RPC 看门狗脚本的静态护栏(2026-10-13)。

看门狗是部署到 Windows 小主机的运维件(PowerShell), 不能在 CI 里真跑, 故以**源码契约**
回归守住两条铁律:
- **纯 ASCII**: PS 5.1 按 GBK 解析 .ps1, 源码里的中文会把语法改坏(前科) —— 任何
  非 ASCII 字节都是回归;
- **真 JSON-RPC 探活**: 只探端口的写法永远发现不了「假活」(端口 LISTENING 但
  JSON-RPC 空响应), 故必须出现 get_market_snapshot + 现价(Now)校验;
  以及连续 2 轮失败才动作(防登录窗口期误杀)、杀进程 + schtasks 重拉、日志落盘。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_SCRIPT = ROOT / "deploy" / "windows" / "tdxw_watchdog.ps1"
_README = ROOT / "deploy" / "windows" / "README.md"


def _src() -> str:
    assert _SCRIPT.exists(), f"看门狗脚本缺失: {_SCRIPT}"
    return _SCRIPT.read_text(encoding="utf-8")


def test_script_is_pure_ascii():
    """铁律: .ps1 源码必须纯 ASCII(PS 5.1 GBK 解析会吃中文改坏语法)。"""
    raw = _SCRIPT.read_bytes()
    bad = [(i, b) for i, b in enumerate(raw) if b > 127]
    assert not bad, f"发现非 ASCII 字节(前 5): {bad[:5]}"


def test_probe_is_real_jsonrpc_with_price_check():
    """探活必须真发 get_market_snapshot 验有现价(Now), 不能只探端口。"""
    s = _src()
    assert "get_market_snapshot" in s
    assert "17709" in s
    # 现价校验: Now 字段 + 正值判定
    assert "$node.Now" in s
    assert "$price -le 0" in s


def test_threshold_kill_and_relaunch_present():
    s = _src()
    assert "FailThreshold = 2" in s              # 连续 2 轮失败才动作
    assert "taskkill.exe /F /IM TdxW.exe /T" in s
    assert "schtasks.exe /run /tn $TaskName" in s
    assert "TdxAutoStart" in s
    # 登录窗口期防误杀: 主窗口存在则不杀
    assert "MainWindowHandle" in s


def test_log_and_state_paths():
    s = _src()
    assert "tqwatch.log" in s                    # 要求写日志到该路径
    assert "qwatch_state.txt" in s               # 连续失败计数持久化
    assert "tdxw-watchdog" in s


def test_dryrun_switch_and_readme_self_test():
    s = _src()
    assert "-DryRun" in s and "switch" in s
    # 安装说明(含 schtasks 5min)必须随源码落库
    assert _README.exists()
    md = _README.read_text(encoding="utf-8")
    assert "schtasks /create" in md and "/mo 5" in md
    assert "-DryRun" in md and "</dev/null" in md  # 内层命令防 stdin 吞流
    assert "TqKeepAlive" in md                     # 替换只探端口的旧逻辑
