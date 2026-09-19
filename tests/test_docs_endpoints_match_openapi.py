"""钉子: 开发者文档里列的每个端点**必须真实存在**。

由来(2026-09-19): 给文档补因子端点时, 我顺手写了一条 `/api/recommendations/strategy-factor-eval` ——
**这个端点根本不存在**(真实的是 `strategy-factors/{signal_run_id}`)。文档写错端点比不写更糟:
外部接 API 的人照着调, 只会拿到 404 并以为是我们服务挂了。

做法: 从 `Developers.tsx` 里抽出所有 `path: '/api...'`, 与 `app.openapi()['paths']` 逐一比对
(路径参数名归一成 `{x}` 再比, 免得只因占位符命名不同就误报)。
"""
from __future__ import annotations

import re
from pathlib import Path

from src.web.app import app

DOC = Path(__file__).resolve().parents[1] / "frontend" / "src" / "pages" / "Developers.tsx"


def _norm(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{x}", path.split("?")[0].rstrip("/"))


def test_documented_endpoints_exist():
    src = DOC.read_text(encoding="utf-8")
    listed = re.findall(r"path: '(/api[^']*)'", src)
    assert listed, "没从文档里解析到端点 —— 检查 Developers.tsx 的写法是否变了"

    real = {_norm(p) for p in app.openapi()["paths"]}
    missing = [p for p in listed if _norm(p) not in real]
    assert missing == [], f"文档里这些端点不存在(照调只会 404): {missing}"


def test_doc_lists_factor_endpoints():
    """B9: 因子能力必须在文档里可发现 —— 后端早就有, 文档不列等于外部看不见。"""
    src = DOC.read_text(encoding="utf-8")
    for p in ("/api/recommendations/strategy-factor-ic", "/api/factors/weights"):
        assert p in src, f"文档缺少 {p}"
