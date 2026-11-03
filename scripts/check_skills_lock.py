#!/usr/bin/env python3
"""技能版本锁检查/更新脚本 (2026-11-03)。

用法:
    python scripts/check_skills_lock.py          # --check 默认模式：有差异 exit 1 并打印差异
    python scripts/check_skills_lock.py --write    # 重算并写回 skills.lock.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

# 确保能导入 src
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from harness.skills import verify_lock, _SKILL_DIR, _LOCK_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description="Skill lock verification and generator")
    parser.add_argument("--write", action="store_true", help="Recompute and write back skills.lock.json")
    args = parser.parse_args()

    if args.write:
        lock_data = {}
        if _SKILL_DIR.exists() and _SKILL_DIR.is_dir():
            for p in sorted(_SKILL_DIR.glob("*.md")):
                name = p.stem
                content = p.read_bytes()
                text = content.decode("utf-8")
                sha = hashlib.sha1(content).hexdigest()
                chars = len(text)
                lock_data[name] = {"sha1": sha, "chars": chars}
        
        _LOCK_PATH.write_text(
            json.dumps(lock_data, indent=2, sort_keys=True) + "\n",
            encoding="utf-8"
        )
        print(f"✅ 已成功更新并写入锁文件: {_LOCK_PATH} (包含 {len(lock_data)} 篇技能)")
        sys.exit(0)
    else:
        issues = verify_lock()
        if issues:
            print("❌ 技能版本锁校验失败 (发现以下不一致):")
            for issue in issues:
                print(f"  - {issue}")
            print("\n如确有更新，请运行: python scripts/check_skills_lock.py --write")
            sys.exit(1)
        else:
            print("✅ 技能版本锁校验通过：所有磁盘技能与 skills.lock.json 完全吻合。")
            sys.exit(0)


if __name__ == "__main__":
    main()
