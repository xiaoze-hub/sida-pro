#!/usr/bin/env python3
"""W3.1(D2, 2026-09-09) 静态门禁: ``IS_PG`` 只允许出现在方言层 src/db/ 内。

此前 IS_PG 模块级布尔散落 6 个文件 19 处, 业务代码各自手写 PG/SQLite SQL 分叉。
收编进 src/db/dialect.py 后, 业务代码一律走 is_postgres() / declared_backend() /
upsert_sql() / insert_ignore_sql(); 本门禁防止 IS_PG(含 from-import)再次外溢。

用法: python scripts/check_is_pg_scope.py            # 门禁模式
      python scripts/check_is_pg_scope.py --list     # 同输出, 永远退出 0
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_ROOT = REPO_ROOT / "src" / "db"
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "frontend",
    "attic",
    "docs",
}
PATTERN = re.compile(r"\bIS_PG\b")
SELF = Path(__file__).resolve()


def find_violations(root: Path) -> list[str]:
    """返回违规 "相对路径:行号: 行内容" 列表(root 一般为仓库根)。"""
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.resolve() == SELF:
            continue
        if any(part in EXCLUDE_DIRS for part in path.parts):
            continue
        try:
            path.resolve().relative_to(ALLOWED_ROOT)
        except ValueError:
            pass
        else:
            continue
        rel = path.relative_to(root)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if PATTERN.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:120]}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只列出, 不当失败(存量盘点用)")
    args = ap.parse_args()

    violations = find_violations(REPO_ROOT)
    if not violations:
        print("check_is_pg_scope: OK — IS_PG 只存在于 src/db/ 方言层")
        return 0
    print(f"check_is_pg_scope: {len(violations)} 处 IS_PG 外溢(只允许 src/db/):")
    for v in violations:
        print(f"  {v}")
    if args.list:
        return 0
    print(
        "修复方式: 改用 from src.db.dialect import "
        "is_postgres/declared_backend/upsert_sql/insert_ignore_sql"
    )
    return 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
