"""实验记录（P0-0.5）：把「改了什么 → 指标怎么变」落库，供人工 accept/reject。

**为什么不复用 copilot_memories**：那边存用户偏好（口径喜好、常看标的），
这边存配方实验。混表会让偏好检索被实验噪音污染，且两者生命周期完全不同。

**为什么只存事实**：`change_summary` 写"把涨停梯队从 2 段拆成 3 段"，
不写"效果更好了"；好坏交给 `verdict` 字段（来自对照数据，不是叙述）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

VERDICTS = ("keep", "rollback")


def record(
    db,
    *,
    recipe_id: str,
    change_summary: str,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    verdict: str,
    reasons: list[str] | None = None,
    evidence_path: str | None = None,
    user_id: str | None = None,
    note: str | None = None,
):
    """写一条实验。verdict 非法直接抛 —— 宁可报错，不要产生无法判读的历史。"""
    from src.db.models import HarnessExperiment

    if verdict not in VERDICTS:
        raise ValueError(f"verdict 必须是 {VERDICTS}，收到 {verdict!r}")
    if not (change_summary or "").strip():
        raise ValueError("change_summary 不能为空：没有'改了什么'的实验记录无法复核")

    row = HarnessExperiment(
        user_id=user_id,
        recipe_id=recipe_id,
        change_summary=change_summary.strip(),
        baseline=baseline or {},
        candidate=candidate or {},
        verdict=verdict,
        reasons=list(reasons or []),
        evidence_path=evidence_path,
        note=note,
        created_at=datetime.utcnow(),
    )
    db.add(row)
    try:
        db.commit()
    except Exception:
        from src.core.db_safe import safe_rollback

        safe_rollback(db)
        raise
    return row


def record_compare(db, result: dict, *, recipe_id: str, change_summary: str,
                   evidence_path: str | None = None, user_id: str | None = None):
    """直接吃 `harness_d3_probe.compare()` 的返回，避免调用方手工搬字段出错。"""
    decision = result.get("decision") or {}
    return record(
        db,
        recipe_id=recipe_id,
        change_summary=change_summary,
        baseline=result.get("baseline") or {},
        candidate=result.get("candidate") or {},
        verdict=decision.get("verdict") or "rollback",
        reasons=decision.get("reasons") or [],
        evidence_path=evidence_path,
        user_id=user_id,
    )


def list_recent(db, *, limit: int = 20, user_id: str | None = None, recipe_id: str | None = None):
    from src.db.models import HarnessExperiment

    q = db.query(HarnessExperiment)
    if user_id is not None:
        q = q.filter(HarnessExperiment.user_id == user_id)
    if recipe_id:
        q = q.filter(HarnessExperiment.recipe_id == recipe_id)
    return q.order_by(HarnessExperiment.created_at.desc()).limit(max(1, min(limit, 100))).all()


def verdict_stats(db, *, user_id: str | None = None) -> dict[str, int]:
    """保留/回滚计数。空库返回 0，不编造"暂无数据"以外的任何数字。"""
    from src.db.models import HarnessExperiment

    q = db.query(HarnessExperiment)
    if user_id is not None:
        q = q.filter(HarnessExperiment.user_id == user_id)
    rows = q.all()
    stats = {v: 0 for v in VERDICTS}
    for r in rows:
        if r.verdict in stats:
            stats[r.verdict] += 1
    stats["total"] = len(rows)
    return stats
