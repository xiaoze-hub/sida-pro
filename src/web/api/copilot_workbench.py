"""参谋工作台写入型接口（2026-10-25）。

为什么单开一个模块而不是塞进 `src/web/api/copilot.py`：后者承担只读视图，
且 `src/web/api/chat.py` 有 2310 行硬护栏——所有"新增长期逻辑"一律落新文件。

本模块当前只做一件事：**让用户选择参谋用哪个模型**。

为什么不是"只加个下拉就好"：`chat.py` 的 `SendMessageBody` 没有 model 字段，
改它需要动 chat.py（行数护栏挡住）。但 chat.py 组装客户端时走的是
`get_model_for_scene(db, "chat", user=user)`——所以只要把"用户选中"这一层
做在解析器里（`user_ai_prefs` 表），选择就自然生效，chat.py 一行不用改。

越权防线：只能在**该用户已获授权**的模型里选。解析时按 `users.permissions.model_access`
校验，deny_all 直接失效、granted 只认 model_ids 内的模型。用户选不了自己没权限的模型。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.web.api._scope import allow_cross_user
from src.web.api.auth import get_current_user
from src.web.api.copilot_jobs import run_user_scope_ok
from src.web.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/copilot", tags=["copilot-workbench"])

# 参谋对话固定走 chat 场景（与 chat.py 的 get_model_for_scene(db, "chat", ...) 一致）
SCENE = "chat"


class ModelChoiceBody(BaseModel):
    # None = 清除选择，回到"跟随场景绑定"
    model_id: int | None = None


def _visible_models(db: Session, user: Any) -> list[dict]:
    """该用户可选/可见的模型清单（与解析器同一套授权口径）。

    这里不返回 api_key / base_url —— 前端只需要展示与选择，凭证不出后端。
    """
    from src.core.ai_client import _get_model_access  # noqa: PLC2701
    from src.db.models import AIModel, AIService

    access = _get_model_access(user) or {}
    mode = str(access.get("mode") or "inherit")
    if mode == "deny_all":
        return []

    q = db.query(AIModel)
    if mode == "granted":
        ids = [int(i) for i in (access.get("model_ids") or []) if str(i).isdigit()]
        if not ids:
            return []
        q = q.filter(AIModel.id.in_(ids))

    rows = q.order_by(AIModel.is_default.desc(), AIModel.id.asc()).all()
    svc_names = {
        s.id: s.name
        for s in db.query(AIService).filter(
            AIService.id.in_({r.service_id for r in rows if r.service_id})
        )
    }
    return [
        {
            "id": r.id,
            "model": r.model,
            "service": svc_names.get(r.service_id) or "--",
            "is_default": bool(r.is_default),
        }
        for r in rows
    ]


def _current_selection(db: Session, user: Any) -> dict:
    """当前生效的模型 + 来源（pref / scene / default）。

    `source` 必须如实说：用户看到"跟随场景绑定"就不能实际在用某个偏好模型。
    """
    from src.core.ai_client import get_model_for_scene, resolve_scene_model_with_source

    resolved = resolve_scene_model_with_source(db, SCENE, user=user)
    if resolved is not None:
        return resolved

    # 兜底：解析器未给出结构化结果时，至少把模型名报出去（不猜来源）
    model = get_model_for_scene(db, SCENE, user=user)
    if model is None:
        return {"source": "none", "model_id": None, "model": None, "service": None}
    if isinstance(model, dict):
        return {
            "source": "byok",
            "model_id": None,
            "model": model.get("model"),
            "service": model.get("name"),
        }
    return {
        "source": "scene",
        "model_id": getattr(model, "id", None),
        "model": getattr(model, "model", None),
        "service": None,
    }


@router.get("/model")
def get_copilot_model(
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """参谋页模型选择器数据：当前生效模型 + 来源 + 可选清单。"""
    return {
        "scene": SCENE,
        "current": _current_selection(db, user),
        "models": _visible_models(db, user),
    }


@router.put("/model")
def set_copilot_model(
    body: ModelChoiceBody,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """设置/清除本人在 chat 场景的模型选择。

    - `model_id=None` → 清除偏好，回到跟随场景绑定
    - 不在可见清单内 → 403（越权，不落库）
    - demo/guest → 403（与解析器一致：零授权）
    """
    from src.core.ai_client import _get_model_access  # noqa: PLC2701
    from src.core.copilot_model_prefs import clear_pref, set_pref

    uid = getattr(user, "id", None)
    if uid is None:
        raise HTTPException(status_code=401, detail="未登录")

    username = getattr(user, "username", None)
    role = getattr(user, "role", None)
    if username == "demo" or role == "guest":
        raise HTTPException(status_code=403, detail="访客账户不能选择模型")

    if body.model_id is None:
        clear_pref(db, uid, SCENE)
        return {"ok": True, "current": _current_selection(db, user)}

    allowed = {m["id"] for m in _visible_models(db, user)}
    if body.model_id not in allowed:
        access = _get_model_access(user) or {}
        raise HTTPException(
            status_code=403,
            detail=f"模型 {body.model_id} 不在你可用的模型范围内"
            f"（授权模式 {access.get('mode') or 'inherit'}）",
        )

    try:
        set_pref(db, uid, SCENE, int(body.model_id))
    except Exception as exc:  # noqa: BLE001 —— 写库失败必须回滚, 否则会话脏
        from src.core.db_safe import safe_rollback

        safe_rollback(db)
        logger.warning("保存模型偏好失败: %s", exc)
        raise HTTPException(status_code=500, detail="保存模型选择失败") from exc

    return {"ok": True, "current": _current_selection(db, user)}


# ──────────────── 并行诊断（多子 run） ────────────────
# 一个提问 → N 个子 run，每个子 run 一条轨迹（G1 可回放），预算各自独立。
#
# ⚠️ 为什么不是"勾选配方": 配方路由在 chat.py 内自动完成（route_for_request 只收工具白名单，
# 不收配方 id），要强制指定就得改 chat.py（2310 行护栏挡住）。所以这里**不假装**能选配方：
# 用户给 N 个子问题，每个子 run 自动路由，面板如实显示各自**实际命中**了哪个配方。


class ParallelItem(BaseModel):
    question: str


class ParallelBody(BaseModel):
    items: list[ParallelItem]
    budget: int | None = None


# 单次最多 4 个子任务：串行执行（delegate_many 是串行，避免上游 429），
# 再多就是让用户干等 —— 上限卡在这里比事后解释便宜。
MAX_PARALLEL_ITEMS = 4


def _child_executor(user_id: str):
    """把异步执行体包成 delegate() 要的同步 executor（在独立线程里跑）。"""

    @allow_cross_user  # 后台执行体按 run_id 读轨迹, 没有请求上下文; run 归属在提交/查询时已校验
    def _run(*, run_id: str, recipe_id: str, question: str, budget: int | None) -> dict:
        import asyncio
        import time

        from src.db.models import CopilotRun
        from src.db.session import SessionLocal
        from src.web.api import copilot_jobs

        t0 = time.time()
        asyncio.run(
            copilot_jobs.run_copilot_question(
                user_id=user_id, conversation_id=None, question=question, run_id=run_id
            )
        )
        elapsed = int((time.time() - t0) * 1000)
        # tool_calls 以轨迹行里的实际计数为准（不猜）
        db = SessionLocal()
        try:
            row = db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
            return {"tool_calls": int(getattr(row, "tool_calls", 0) or 0), "elapsed_ms": elapsed}
        finally:
            db.close()

    return _run


@router.post("/parallel")
async def submit_parallel(
    body: ParallelBody,
    user: Any = Depends(get_current_user),
) -> dict:
    """提交并行诊断：立即返回 parent run_id，子 run 在后台串行执行。"""
    import asyncio
    import uuid

    from src.core import copilot_quota as quota
    from src.core.delegate import delegate_many
    from src.db.models import CopilotRun
    from src.db.session import SessionLocal

    items = [i.question.strip() for i in (body.items or []) if i.question and i.question.strip()]
    if not items:
        raise HTTPException(status_code=400, detail="至少要一个子问题")
    if len(items) > MAX_PARALLEL_ITEMS:
        raise HTTPException(
            status_code=400,
            detail=f"最多 {MAX_PARALLEL_ITEMS} 个子问题（当前 {len(items)} 个）",
        )

    uid = getattr(user, "id", None)
    if uid is None:
        raise HTTPException(status_code=401, detail="未登录")

    parent_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        quota.enforce_run_concurrency(db, user)  # 复用既有并发上限，超限 429
        db.add(
            CopilotRun(
                run_id=parent_id,
                conversation_id=None,
                user_id=str(uid),
                recipe="parallel",
                question=f"并行诊断 · {len(items)} 个子任务",
                status="queued",
                tool_calls=0,
            )
        )
        db.commit()
    except HTTPException:
        from src.core.db_safe import safe_rollback

        safe_rollback(db)
        raise
    except Exception as exc:  # noqa: BLE001
        from src.core.db_safe import safe_rollback

        safe_rollback(db)
        logger.warning("创建并行诊断父 run 失败: %s", exc)
        raise HTTPException(status_code=500, detail="创建任务失败") from exc
    finally:
        db.close()

    def _work() -> None:
        """后台线程：建 N 个子 run → 串行执行 → 汇总。"""
        from src.db.session import SessionLocal as SL

        wdb = SL()
        try:
            _mark_parent(parent_id, status="running")
            delegate_many(
                wdb,
                parent_run_id=parent_id,
                user_id=str(uid),
                tasks=[("", q) for q in items],  # 配方留空 = 由路由决定（不假装能指定）
                executor=_child_executor(str(uid)),
                budget=body.budget,
            )
            _mark_parent(parent_id, status="ok")
        except Exception as exc:  # noqa: BLE001
            logger.warning("并行诊断执行失败 parent=%s: %s", parent_id, exc)
            _mark_parent(parent_id, status="failed", error=f"{type(exc).__name__}: {exc}"[:400])
        finally:
            wdb.close()

    asyncio.get_running_loop().run_in_executor(None, _work)
    return {"run_id": parent_id, "status": "queued", "count": len(items)}


@allow_cross_user  # 后台线程按 run_id 写父 run 状态, 无请求上下文; 归属在提交端校验
def _mark_parent(run_id: str, *, status: str, error: str | None = None) -> None:
    from datetime import datetime

    from src.db.models import CopilotRun
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        row = db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
        if row is None:
            return
        row.status = status
        row.error = error
        if status in ("ok", "failed"):
            row.finished_at = datetime.utcnow()
        db.commit()
    except Exception:  # noqa: BLE001
        from src.core.db_safe import safe_rollback

        safe_rollback(db)
    finally:
        db.close()


@router.get("/parallel/{run_id}")
def get_parallel(
    run_id: str,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """并行诊断进度：父状态 + 每个子 run 的事实 + 缺口汇总。"""
    from src.core.delegate import aggregate
    from src.db.models import CopilotRun

    row = db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
    if row is None or not run_user_scope_ok(row, user):
        raise HTTPException(status_code=404, detail="任务不存在")

    agg = aggregate(db, run_id)
    return {
        "run_id": run_id,
        "status": row.status,
        "error": row.error,
        "created_at": str(row.created_at or ""),
        "summary": agg,
    }


# ──────────────── 治理可见化(事中口径, 只读) ────────────────
# 背景: 预算/白名单/Frozen Ring 现在全是**事后**才知道(CI 或跑完看轨迹)。
# 这里把"这一轮到底受什么约束"在运行时就摊开: 预算上限与实际用量、工具白名单条数、
# 哪些东西不在自动改动范围(Frozen Ring)。
#
# 只读, 不做拦截 —— 真正的拦在 chat.py 的 `_REQ_BUDGET` 与 frozen 守卫里, 这里只负责让人看得见。


@router.get("/runs/{run_id}/governance")
def get_run_governance(
    run_id: str,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """这一轮的治理现状：预算(上限/已用)、工具白名单、Frozen Ring、限流档位。

    数据来源都是既有事实（`CopilotRun` 行 + 配方 YAML），不新增埋点、不推测。
    缺值一律 `None`（前端显示 `--`），**不补 0** —— 没记到 ≠ 用了 0 次。
    """
    from src.db.models import CopilotRun
    from src.harness.recipes import load_recipes

    row = db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
    if row is None or not run_user_scope_ok(row, user):
        raise HTTPException(status_code=404, detail="任务不存在")

    recipe = None
    recipe_id = (row.recipe or "").strip()
    if recipe_id:
        try:
            recipe = load_recipes().get(recipe_id)
        except Exception:  # noqa: BLE001 —— 配方读不出来不能把治理看板打死
            logger.debug("读取配方失败: %s", recipe_id, exc_info=True)

    max_calls = getattr(recipe, "max_tool_calls", None) if recipe is not None else None
    tools = list(getattr(recipe, "tools", None) or []) if recipe is not None else []
    used = row.tool_calls

    return {
        "run_id": run_id,
        "status": row.status,
        "recipe": recipe_id or None,
        "budget": {
            "max": int(max_calls) if isinstance(max_calls, int) else None,
            "used": int(used) if isinstance(used, int) else None,
            # 只有两边都有数才算余量；缺一边就是 None，绝不按 0 兜底
            "remaining": (
                int(max_calls) - int(used)
                if isinstance(max_calls, int) and isinstance(used, int)
                else None
            ),
        },
        "whitelist": {
            # 空列表 = 本轮没有配方级白名单（不代表"允许所有"，前端按"未限定"显示）
            "count": len(tools),
            "tools": tools,
        },
        "frozen_ring": {
            "guards": [
                "symbol_guard(标的核对)",
                "number_audit(数字审计)",
                "caliber.py(口径守卫)",
                "交易接口 403 守卫",
            ],
            "note": "以上四条属 Frozen Ring：只读可见，任何自动改动都不得覆盖",
        },
        "limits": {
            "max_tool_calls": int(max_calls) if isinstance(max_calls, int) else None,
            "recipe_tools_declared": len(tools),
        },
        # 事中拦截是**真发生了**才记：这里只回查审计里属于本 run 的记录，
        # 没被拦过就是空列表（不是"没拦"的能力声明）
        "interceptions": _interceptions_for_run(db, run_id, str(getattr(user, "id", ""))),
    }


def _interceptions_for_run(db: Session, run_id: str, user_id: str) -> list[dict]:
    """从审计里捞本 run 的拦截记录（预算超限 / 并发超限）。

    审计 detail 是 `run=<id> used=.. limit=..` 这种半结构化文本，按 `run=<id>` 前缀匹配。
    只查本人：审计表按 user_id 过滤，避免看到别人的拦截。
    """
    from src.db.models import AuditLog

    try:
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.user_id == user_id)
            .filter(AuditLog.action.like("copilot_%"))
            .filter(AuditLog.detail.like(f"%run={run_id}%"))
            .order_by(AuditLog.id.desc())
            .limit(10)
            .all()
        )
    except Exception:  # noqa: BLE001 —— 审计查不出来不能把治理看板打死
        from src.core.db_safe import safe_rollback

        safe_rollback(db)
        logger.debug("查询拦截审计失败: run=%s", run_id, exc_info=True)
        return []
    return [
        {
            "action": r.action,
            "detail": r.detail or "",
            "created_at": str(getattr(r, "created_at", "") or ""),
        }
        for r in rows
    ]


@router.get("/governance/interceptions")
def list_interceptions(
    limit: int = 20,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """本人最近的**事中拦截**记录（预算超限 / 并发超限）。

    只回查已发生的事实：没有记录 = 没被拦过，不代表"不会拦"。
    """
    from src.db.models import AuditLog

    n = max(1, min(100, int(limit)))
    uid = str(getattr(user, "id", "") or "")
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.user_id == uid)
        .filter(AuditLog.action.like("copilot_%"))
        .order_by(AuditLog.id.desc())
        .limit(n)
        .all()
    )
    items = [
        {
            "action": r.action,
            "detail": r.detail or "",
            "created_at": str(getattr(r, "created_at", "") or ""),
        }
        for r in rows
    ]
    return {"count": len(items), "items": items}


# ──────────────── 会话去重（清理测试数据） ────────────────
# 背景：反复测试留下大量同名会话（"上证指数现在多少点" ×7），左栏像调试日志。
# 设计原则：
#   1. **默认 dry-run**：不传 apply 只回报"会删哪些"，绝不静默删数据；
#   2. **按标题分组，每组保留最新 keep 条**（keep>=1，不允许把整组删空）；
#   3. 只动本人会话；删除时连 ChatMessage 一起删（该表没有 FK 级联）；
#   4. 单次上限 MAX_DEDUPE_DELETE，防止一次误删太多 —— 超了就分批来。


class DedupeBody(BaseModel):
    apply: bool = False          # False = 只试运行
    keep: int = 1                # 每个同名组保留最新几条
    min_group: int = 2           # 至少几条才算"重复组"
    max_delete: int = 200        # 本次最多删多少条


@router.post("/conversations/dedupe")
def dedupe_conversations(
    body: DedupeBody,
    db: Session = Depends(get_db),
    user: Any = Depends(get_current_user),
) -> dict:
    """把同名会话去重（保留每组最新的 keep 条）。

    默认 `apply=False` 只出报告。真正删除时返回被删 id 列表，便于事后核对。
    """
    from src.db.models import ChatConversation, ChatMessage

    uid = getattr(user, "id", None)
    if uid is None:
        raise HTTPException(status_code=401, detail="未登录")

    keep = max(1, int(body.keep))
    min_group = max(2, int(body.min_group))
    max_delete = max(1, min(1000, int(body.max_delete)))

    rows = (
        db.query(ChatConversation)
        .filter(ChatConversation.user_id == uid)
        .order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc())
        .all()
    )

    groups: dict[str, list] = {}
    for r in rows:
        title = (r.title or "").strip()
        if not title:
            continue  # 无标题会话不参与（可能是刚建还没命名）
        groups.setdefault(title, []).append(r)

    plan: list[dict] = []
    doomed: list[int] = []
    for title, items in groups.items():
        if len(items) < min_group:
            continue
        # items 已按 updated_at 降序 → 前 keep 条是"最新"，其余删
        removals = items[keep:]
        if not removals:
            continue
        plan.append({"title": title[:60], "total": len(items), "remove": len(removals)})
        doomed.extend([r.id for r in removals])

    plan.sort(key=lambda x: -x["remove"])
    truncated = len(doomed) > max_delete
    doomed = doomed[:max_delete]

    if not body.apply:
        return {
            "apply": False,
            "groups": plan,
            "would_delete": len(doomed),
            "truncated": truncated,
            "note": "试运行：未删除任何数据。确认后传 apply=true 再执行一次",
        }

    if doomed:
        db.query(ChatMessage).filter(ChatMessage.conversation_id.in_(doomed)).delete(
            synchronize_session=False
        )
        # scoped-check: allow — doomed 只来自本函数上方 rows(按 ChatConversation.user_id == uid 过滤)
        db.query(ChatConversation).filter(ChatConversation.id.in_(doomed)).delete(
            synchronize_session=False
        )
        db.commit()

    return {
        "apply": True,
        "groups": plan,
        "deleted": len(doomed),
        "deleted_ids": doomed,
        "truncated": truncated,
    }
