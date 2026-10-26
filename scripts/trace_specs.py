#!/usr/bin/env python3
"""规格追溯门禁 —— 借鉴 self-hosting-agent/scripts/trace.mjs。

双向校验, 任一条不满足即非零退出:
  1. 规格有验收标准, 却没有任何测试引用它   → 文档里的空头承诺
  2. 测试引用了不存在的规格 ID             → 测试在验证幻觉
  3. 每个规格文件至少识别出 1 条标准       → 门禁自己骗人

用法: python scripts/trace_specs.py [--quiet]
产物: specs/traceability.md (自动生成, 请勿手工修改)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPECS_DIR = ROOT / "specs"
TESTS_DIR = ROOT / "tests"
OUT_MD = SPECS_DIR / "traceability.md"

# ⚠️ 前缀 2-8 位(字母+数字), 后接三位序号。
#    前缀长度必须与实际规格 ID 命名一致 —— self-hosting-agent 历史上
#    因正则只认到 6 位而漏掉 7 位的 "BROWSER-", 整条规格对门禁隐形,
#    门禁却仍报「全部覆盖」。**门禁骗人比没有门禁更危险**。
ID_PATTERN = r"[A-Z][A-Z0-9]{1,7}-\d{3}"

_CRITERION_RE = re.compile(rf"^\s*-\s+\*\*({ID_PATTERN})\*\*\s*(.+?)\s*$")
_TEST_SPEC_RE = re.compile(rf"@spec\s+({ID_PATTERN})")


def walk(base: Path, keep) -> list[Path]:
    if not base.exists():
        return []
    out: list[Path] = []
    for p in sorted(base.rglob("*")):
        if p.is_file() and keep(p):
            out.append(p)
    return out


def collect_criteria() -> dict[str, dict]:
    """从 specs/**/*.md 收集验收标准。"""
    criteria: dict[str, dict] = {}
    files = walk(SPECS_DIR, lambda p: p.suffix == ".md" and p.name != OUT_MD.name)
    for f in files:
        rel = f.relative_to(ROOT)
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            m = _CRITERION_RE.match(line)
            if not m:
                continue
            sid, summary = m.group(1), m.group(2).strip()
            if sid in criteria:
                raise SystemExit(
                    f"✗ 验收标准 ID 重复: {sid}\n"
                    f"    {criteria[sid]['spec']}:{criteria[sid]['line']}\n"
                    f"    {rel}:{i}"
                )
            criteria[sid] = {"id": sid, "summary": summary, "spec": str(rel), "line": i}

    # ★ 自检: 某个规格文件一条标准都没识别出来 → 多半是 ID 命名超出正则。
    #   这条自检本身是有测试守着的(specs/**/trace.md 里)。
    for f in files:
        rel = f.relative_to(ROOT)
        n = sum(1 for c in criteria.values() if c["spec"] == str(rel))
        if n == 0:
            raise SystemExit(
                f"✗ 规格文件一条验收标准都没识别出来: {rel}\n"
                f"    检查 ID 命名是否超出正则 {ID_PATTERN}\n"
                f"    (历史事故: 正则只认到 6 位前缀, 7 位前缀整条规格隐形, 门禁却报「全部覆盖」)"
            )
    return criteria


def collect_tests() -> list[dict]:
    """从 tests/** 收集测试用例并绑定 @spec ID。

    用 **AST** 解析而不是正则扫行 —— 因为 @spec 通常写在测试函数的
    **docstring 里**(def 之后), 正则扫行容易在函数体第一行就把 pending 清空,
    导致"明明写了规格"的测试被判成无守卫。

    识别两种写法:
      - 测试函数的 docstring 里(推荐)
      - 函数**上一行**的注释里
    """
    import ast

    tests: list[dict] = []
    for f in walk(TESTS_DIR, lambda p: p.suffix == ".py"):
        rel = f.relative_to(ROOT)
        src = f.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines()

        # 函数上一行的注释里也认 @spec
        def _comment_specs(lineno: int) -> list[str]:
            # lineno 是 1-based; 往上找最近的注释块
            out: list[str] = []
            j = lineno - 2  # 0-based 上一行
            while j >= 0 and lines[j].strip().startswith("#"):
                out += [m.group(1) for m in _TEST_SPEC_RE.finditer(lines[j])]
                j -= 1
            return out

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            ids: list[str] = []
            doc = ast.get_docstring(node, clean=False) or ""
            ids += [m.group(1) for m in _TEST_SPEC_RE.finditer(doc)]
            ids += _comment_specs(node.lineno)
            for sid in dict.fromkeys(ids):
                tests.append({
                    "id": sid,
                    "name": node.name,
                    "file": str(rel),
                    "line": node.lineno,
                })
    return tests


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    criteria = collect_criteria()
    tests = collect_tests()

    covered: dict[str, list[dict]] = {}
    unknown: list[dict] = []
    for t in tests:
        if t["id"] in criteria:
            covered.setdefault(t["id"], []).append(t)
        else:
            unknown.append(t)

    problems: list[str] = []

    # 违规 1: 规格有标准但没有测试
    uncovered = sorted(set(criteria) - set(covered))
    for sid in uncovered:
        c = criteria[sid]
        problems.append(f"规格 {sid} 没有测试守着 —— {c['spec']}:{c['line']} | {c['summary']}")

    # 违规 2: 测试引用了不存在的规格 ID(测试在验证幻觉)
    for t in unknown:
        problems.append(f"测试引用了不存在的规格 ID {t['id']} —— {t['file']}:{t['line']} ({t['name']})")

    total = len(criteria)
    pct = (total - len(uncovered)) / total * 100 if total else 0.0

    if not args.quiet:
        print(f"规格验收标准 {total} 条, 已覆盖 {total - len(uncovered)} 条 ({pct:.1f}%)")
        print(f"测试用例 {len(tests)} 个, 绑定规格 {sum(len(v) for v in covered.values())} 次")

    # 产出追溯表
    lines = [
        "# 规格追溯表 (自动生成, 请勿手工修改)",
        "",
        f"- 验收标准: **{total}** 条",
        f"- 已覆盖: **{total - len(uncovered)}** 条 ({pct:.1f}%)",
        f"- 测试用例: **{len(tests)}** 个",
        "",
        "| 规格 ID | 摘要 | 规格位置 | 守卫测试 |",
        "|---|---|---|---|",
    ]
    for sid in sorted(criteria):
        c = criteria[sid]
        guards = covered.get(sid, [])
        g = "<br>".join(f"`{t['file']}:{t['line']}` `{t['name']}`" for t in guards) or "**❌ 无**"
        lines.append(f"| `{sid}` | {c['summary']} | `{c['spec']}:{c['line']}` | {g} |")
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    if problems:
        print(f"\n✗ 规格追溯门禁未通过, {len(problems)} 项问题:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print("✔ 规格追溯门禁通过: 每条验收标准都有测试守着。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
