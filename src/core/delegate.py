"""委派子 run（P1-1.1, 2026-10-25）。

**为什么委派不是"再开一个自由 agent"**：
自由 agent 会自己挑工具、自己定口径，父 run 拿到一堆无法归因的结论。
这里的 `delegate()` 走**同一条管线、同一套口径**，只换配方与问题，
子 run 带 `parent_run_id`、有**独立预算**，父 run 只汇总证据与缺口。

三条硬约束（写死在实现里，不靠调用方自觉）：
1. **不新增 chat 工具** —— 委派是内部能力，不是模型可选动作（注册表 77 是钉子）；
2. **子 run 预算独立** —— 子 run 的工具调用不扣父 run 的预算，否则一次委派就能把父吃穿；
3. **失败不自动重跑** —— 重跑会烧额度，而且用户可能已经看到半截答案（与 Durable Run 一致）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Callable, Iterable

# 子 run 默认预算（工具调用次数）。比父 run 小：子任务范围窄，给太多等于鼓励它乱翻。
DEFAULT_CHILD_BUDGET = 6


def _new_run_id() -> str:
    return str(uuid.uuid4())


def create_child_run(
    db,
    *,
    parent_run_id: str,
    user_id: str | None,
    recipe_id: str,
    question: str,
    budget: int | None = None,
) -> dict:
    """建一条 queued 的子 run 行（带 parent_run_id）。

    先建行再执行：执行体崩了也能在库里看到"这个子任务存在过"，不会变成幽灵。
    """
    from src.db.models import CopilotRun

    budget = DEFAULT_CHILD_BUDGET if budget is None else max(1, int(budget))
    run = CopilotRun(
        run_id=_new_run_id(),
        conversation_id=None,
        user_id=user_id,
        recipe=recipe_id,
        question=(question or "")[:500],
        status="queued",
        tool_calls=0,
        parent_run_id=parent_run_id,
        created_at=datetime.utcnow(),
    )
    db.add(run)
    db.commit()
    return {"run_id": run.run_id, "recipe_id": recipe_id, "budget": budget, "status": "queued"}


def delegate(
    db,
    *,
    parent_run_id: str,
    user_id: str | None,
    recipe_id: str,
    question: str,
    executor: Callable[..., Any] | None = None,
    budget: int | None = None,
) -> dict:
    """建子 run → 执行 → 回填结论。返回子 run 的事实（不返回"感觉不错"这种话）。

    `executor` 可注入：生产传真正执行体，测试传 stub —— 这样测试不需要 LLM，
    也不会污染配额（与 `copilot_jobs.run_copilot_question` 同一套思路）。
    """
    from src.db.models import CopilotRun

    child = create_child_run(
        db, parent_run_id=parent_run_id, user_id=user_id,
        recipe_id=recipe_id, question=question, budget=budget,
    )
    run_id = child["run_id"]

    if executor is None:
        # 没有执行体也**不假装成功**：子 run 落 failed 并注明原因
        _finish(db, run_id, status="failed", error="没有可用的执行体，子任务未执行")
        return {**child, "status": "failed", "error": "没有可用的执行体，子任务未执行"}

    _mark_running(db, run_id)
    try:
        result = executor(
            run_id=run_id, recipe_id=recipe_id, question=question, budget=child["budget"],
        )
    except Exception as e:  # noqa: BLE001
        # 失败不重跑：如实落 failed，父 run 汇总时会看到这块证据缺失
        msg = f"{type(e).__name__}: {e}"
        _finish(db, run_id, status="failed", error=msg[:500])
        return {**child, "status": "failed", "error": msg}

    if isinstance(result, dict) and result.get("error"):
        msg = str(result["error"])[:500]
        _finish(db, run_id, status="failed", error=msg)
        return {**child, "status": "failed", "error": msg}

    tool_calls = int((result or {}).get("tool_calls") or 0) if isinstance(result, dict) else 0
    elapsed = int((result or {}).get("elapsed_ms") or 0) if isinstance(result, dict) else 0
    _finish(db, run_id, status="ok", tool_calls=tool_calls, elapsed_ms=elapsed)
    return {**child, "status": "ok", "tool_calls": tool_calls, "elapsed_ms": elapsed}


def _mark_running(db, run_id: str) -> None:
    from src.db.models import CopilotRun

    row = db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
    if row is None:
        return
    row.status = "running"
    row.heartbeat_at = datetime.utcnow()
    db.commit()


def _finish(db, run_id: str, *, status: str, error: str | None = None,
            tool_calls: int = 0, elapsed_ms: int = 0) -> None:
    from src.db.models import CopilotRun

    row = db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
    if row is None:
        return
    row.status = status
    row.error = error
    row.tool_calls = tool_calls
    row.elapsed_ms = elapsed_ms or None
    row.finished_at = datetime.utcnow()
    db.commit()


def children_of(db, parent_run_id: str) -> list[dict]:
    """父 run 的子任务清单（按创建顺序）。"""
    from src.db.models import CopilotRun

    rows = (
        db.query(CopilotRun)
        .filter(CopilotRun.parent_run_id == parent_run_id)
        .order_by(CopilotRun.id.asc())
        .all()
    )
    return [
        {
            "run_id": r.run_id,
            "recipe_id": r.recipe,
            "status": r.status,
            "tool_calls": r.tool_calls,
            "elapsed_ms": r.elapsed_ms,
            "error": r.error,
            "question": (r.question or "")[:120],
        }
        for r in rows
    ]


def aggregate(db, parent_run_id: str) -> dict:
    """父 run 的证据汇总：**只汇总事实与缺口**，不重新执行、不替子任务重跑。

    返回 `available=False` 是常态（父 run 没委派过），不是错误。
    """
    kids = children_of(db, parent_run_id)
    if not kids:
        return {"available": False, "reason": "这轮没有委派子任务", "children": [], "count": 0,
                "ok": 0, "failed": 0, "tool_calls": 0, "gaps": []}

    ok = [k for k in kids if k["status"] == "ok"]
    failed = [k for k in kids if k["status"] == "failed"]
    pending = [k for k in kids if k["status"] in ("queued", "running")]
    gaps = [
        f"{k['recipe_id']} 未完成：{k['error'] or k['status']}" for k in (failed + pending)
    ]
    return {
        "available": True,
        "reason": "",
        "children": kids,
        "count": len(kids),
        "ok": len(ok),
        # 子 run 的工具调用**单独累计**，不并入父 run 的 tool_calls（预算独立）
        "tool_calls": sum(int(k.get("tool_calls") or 0) for k in ok),
        "failed": len(failed),
        "pending": len(pending),
        "gaps": gaps,
    }


def delegate_many(
    db,
    *,
    parent_run_id: str,
    user_id: str | None,
    tasks: Iterable[tuple[str, str]],
    executor: Callable[..., Any] | None = None,
    budget: int | None = None,
) -> dict:
    """按 (recipe_id, question) 清单依次委派，返回全部子 run + 汇总。

    **串行而不是并发**：子 run 共用同一个 DB session，并发写会让事务互相干扰；
    而且并发会撞上游限流（2026-10-25 实测 7 路并发全撞 HTTP 429）。
    """
    out = []
    for recipe_id, question in tasks:
        out.append(delegate(
            db, parent_run_id=parent_run_id, user_id=user_id,
            recipe_id=recipe_id, question=question, executor=executor, budget=budget,
        ))
    return {"children": out, "summary": aggregate(db, parent_run_id)}
