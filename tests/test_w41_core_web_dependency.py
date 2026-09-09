"""B4.1/KI-039: core→web 反向依赖棘轮门禁 —— 只许减少, 禁止新增。

背景: src/core 直接 import src/web(ORM/SessionLocal) 共 57 个文件(146 处),
核心逻辑无法脱离 Web 层单测, ORM 变更牵动全局。本门禁用**冻结允许清单** +
棘轮, 阻止债务继续扩大; 存量按波次下沉到 src/db/repository。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOWLIST = ROOT / "tests" / "fixtures" / "core_web_deps_allowlist.txt"
PATTERN = re.compile(r"^\s*(from\s+src\.web|import\s+src\.web)", re.M)


def _current_offenders() -> set[str]:
    out: set[str] = set()
    for p in (ROOT / "src" / "core").rglob("*.py"):
        text = p.read_text(encoding="utf-8", errors="ignore")
        if PATTERN.search(text):
            out.add(p.relative_to(ROOT).as_posix())
    return out


def test_no_new_core_to_web_dependencies():
    allowed = {
        ln.strip()
        for ln in ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    }
    current = _current_offenders()
    new = sorted(current - allowed)
    assert not new, (
        "src/core 新增了对 src/web 的反向依赖(B4.1 禁止; 数据访问请下沉到 "
        "src/db/repository): " + ", ".join(new)
    )
    # 棘轮: 存量只许减少(清掉一个就把它从 allowlist 删掉)
    assert len(current) <= len(allowed), (
        f"core→web 依赖文件数 {len(current)} 超过冻结基线 {len(allowed)}"
    )
