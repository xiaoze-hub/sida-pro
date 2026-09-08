#!/usr/bin/env python3
"""W2.5/E6 漂移守卫: requirements.txt 新增顶层依赖而未重新生成 requirements-lock.txt 时, CI 立刻红。

只查"名字覆盖", 不做版本比较 —— lock 是从生产容器 freeze 生成的精确版本,
版本一致性由再生成流程保证; 这里兜住的是"加了依赖忘更新 lock"这个高频事故。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQS = ROOT / "requirements.txt"
LOCK = ROOT / "requirements-lock.txt"

# name 提取: 处理 name>=x / name==x / name[x,y]>=x / Name_To_Camel
NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def norm(name: str) -> str:
    # PEP 503 规范化: 大小写与 -/_ 等价
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_names(path: Path) -> set[str]:
    names = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # 去行尾注释(要求 # 前有空格, 避免误伤 URL 中的 #)
        line = re.split(r"\s+#", line)[0].strip()
        if line.startswith("-"):
            # -e ./packages/marketdata 等本地/安装选项, 不在 lock 管辖
            continue
        if "@" in line:
            # name @ git+https://... 直链
            line = line.split("@", 1)[0].strip()
        m = NAME_RE.match(line)
        if m:
            names.add(norm(m.group(1)))
    return names


def main() -> int:
    req_names = parse_names(REQS)
    lock_names = parse_names(LOCK)
    missing = sorted(req_names - lock_names)
    if missing:
        print("[E6] requirements-lock.txt 缺少 requirements.txt 的顶层依赖:")
        for name in missing:
            print(f"  - {name}")
        print("请在测试环境验证后, 按 requirements-lock.txt 文件头流程重新生成并提交。")
        return 1
    print(f"[E6] lock 覆盖 OK: requirements.txt {len(req_names)} 个顶层依赖全部在 requirements-lock.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
