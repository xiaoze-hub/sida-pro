#!/usr/bin/env python3
"""汇总 python 侧审计是否含 high/critical, 写 GitHub Actions 输出 high=0|1。

读 pip-audit-summary.txt 与 audit_deps.json(若存在)。
输出到 $GITHUB_OUTPUT: high=1 表示需要建 Issue。
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> int:
    high = 0
    summary = Path("pip-audit-summary.txt")
    if summary.exists() and summary.read_text(encoding="utf-8").strip() not in ("", "none"):
        high = 1
    audit_json = Path("audit_deps.json")
    if audit_json.exists():
        try:
            data = json.loads(audit_json.read_text(encoding="utf-8"))
            if data.get("highest_severity") in ("critical", "high"):
                high = 1
        except Exception:  # noqa: BLE001
            pass
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"high={high}\n")
    print(f"high={high}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
