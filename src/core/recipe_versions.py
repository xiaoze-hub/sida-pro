"""配方版本历史（P2-2.1, 2026-10-25）。

**为什么要有历史**：配方是"改一句提示词就换一种行为"的东西。没有指纹历史，
线上表现变了只能靠猜"是不是昨天那行改的"。

**为什么指纹当版本号**：手写 `version: 3` 必然忘记改，且会与实际内容不同步。
指纹 = YAML 文件内容 sha256 前 8 位，改一个字就变，任何人拿文件都能复核。

**为什么惰性记录**（读目录时顺手落库，而不是发布时钩子）：配方是文件，不是数据库行，
不经过应用写路径；观察式记录最可靠 —— 谁第一个读到新内容，就记一条。
"""

from __future__ import annotations

import json
from datetime import datetime

from src.harness import recipes as R


def sync_versions(db) -> list[dict]:
    """把当前配方的指纹同步进 `recipe_versions`，返回**变了或首次出现**的条目。

    幂等：同一指纹只记一次。返回空列表 = 没有任何变化（不是错误）。
    """
    from src.db.models import RecipeVersion

    changed: list[dict] = []
    for rec in R.list_recipes():
        fp = R.recipe_fingerprint(rec.id)
        if not fp:
            continue
        exists = (
            db.query(RecipeVersion.id)
            .filter(RecipeVersion.recipe_id == rec.id, RecipeVersion.fingerprint == fp)
            .first()
        )
        if exists:
            continue
        db.add(RecipeVersion(
            recipe_id=rec.id,
            fingerprint=fp,
            snapshot=json.dumps(rec.public(), ensure_ascii=False),
            created_at=datetime.utcnow(),
        ))
        changed.append({"recipe_id": rec.id, "fingerprint": fp})
    if changed:
        db.commit()
    return changed


def history(db, recipe_id: str, limit: int = 20) -> list[dict]:
    """某个配方的版本历史（新→旧）。没有记录返回空列表，不编造版本。"""
    from src.db.models import RecipeVersion

    rows = (
        db.query(RecipeVersion)
        .filter(RecipeVersion.recipe_id == (recipe_id or "").strip())
        .order_by(RecipeVersion.id.desc())
        .limit(max(1, min(int(limit or 20), 100)))
        .all()
    )
    return [
        {
            "fingerprint": r.fingerprint,
            "created_at": str(r.created_at or ""),
            "snapshot": json.loads(r.snapshot) if r.snapshot else None,
        }
        for r in rows
    ]
