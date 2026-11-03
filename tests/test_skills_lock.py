"""测试技能锁脚本 (check_skills_lock.py)。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# 仓库根按本文件位置推，绝不写死某个 worktree 路径（写死后 CI 直接 FileNotFoundError）
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_skills_lock_script():
    """测试通过命令行脚本运行 check_skills_lock.py 返回 0 状态码。"""
    cmd = [sys.executable, "scripts/check_skills_lock.py"]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO_ROOT))
    assert res.returncode == 0, f"check_skills_lock.py 校验失败: {res.stdout}\n{res.stderr}"
    assert "通过" in res.stdout or "OK" in res.stdout or "✅" in res.stdout
