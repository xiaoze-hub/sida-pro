#!/usr/bin/env python3
"""解析 pip-audit JSON 输出 → 摘要文本 (供 GitHub Actions Issue 正文)。

用法: python scripts/_parse_pip_audit.py <pip-audit.json> <summary.txt>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: _parse_pip_audit.py <in.json> <out.txt>", file=sys.stderr)
        return 2
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    if not src.exists():
        dst.write_text("none\n", encoding="utf-8")
        return 0
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        dst.write_text(f"parse-error: {e}\n", encoding="utf-8")
        return 0
    deps = data if isinstance(data, list) else data.get("dependencies", [])
    lines: list[str] = []
    for d in deps:
        name = d.get("name", "?")
        ver = d.get("version", "?")
        for v in d.get("vulns") or []:
            desc = (v.get("description") or "")[:120].replace("\n", " ")
            lines.append(f"{name}=={ver}: {v.get('id', '?')} {desc}")
    dst.write_text("\n".join(lines) if lines else "none", encoding="utf-8")
    print(f"pip-audit vulns: {len(lines)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
