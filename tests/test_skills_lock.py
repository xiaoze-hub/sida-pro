"""测试技能锁脚本 (check_skills_lock.py)。"""

from __future__ import annotations

import subprocess
import sys


def test_skills_lock_script():
    """测试通过命令行脚本运行 check_skills_lock.py 返回 0 状态码。"""
    cmd = [sys.executable, "scripts/check_skills_lock.py"]
    res = subprocess.run(cmd, capture_output=True, text=True, cwd="/home/ubuntu/wt/qovest-c-factpack")
    assert res.returncode == 0, f"check_skills_lock.py 校验失败: {res.stdout}\n{res.stderr}"
    assert "通过" in res.stdout or "OK" in res.stdout or "✅" in res.stdout
