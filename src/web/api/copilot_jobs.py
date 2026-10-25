"""参谋长任务异步化（P0-0.2，2026-10-25）。

**要解决的事故**：`POST /conversations/{id}/messages` 是**同步**的 —— 一次深问要等
LLM 跑完才返回，P95 已经到 133.5s。代理超时、用户切页、浏览器断网，整轮就白烧了，
而且库里会留下一个永远 `running` 的 run（这正是 P0-0.1 要收的尸）。

**这一层做的事**：
```
POST /copilot/runs            → 建 queued run，立刻返回 run_id（不等 LLM）
GET  /copilot/runs/{id}       → 轮询状态
GET  /copilot/runs/{id}/events → SSE：状态迁移 + 工具步骤，终态自动关闭
POST /copilot/runs/{id}/cancel → 取消（running→cancelled）
```

**为什么执行体直接调 `chat_api.send_message`**：它是普通协程（不依赖 FastAPI 注入），
在后台 task 里传 user 对象就能跑。这样**不动 chat.py**（该文件有 2310 行硬护栏），
也不复制一份 LLM 管线 —— 复制出来的第二份必然与主路径漂移。

**重启怎么办**：心跳由独立协程每 30s 刷新；进程死掉后心跳停，下次启动
`copilot_runtime.reap_stale_runs()` 把它标 failed 并写"可点重试"。**不自动重跑**。

**不做假异步**：提交立即返回是真的（不 sleep、不排队等结果）；SSE 里每条状态都来自
DB 真实迁移，不按时间假装进度。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.web.api._scope import allow_cross_user
from src.web.api.auth import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/copilot", tags=["copilot-jobs"])

#: 单轮硬超时（秒）。超时标 failed 而不是挂起 —— 挂着比失败更糟：用户以为还在跑。
RUN_TIMEOUT_S = 600
#: 心跳间隔（与 copilot_runtime 保持一致）
HEARTBEAT_S = 30
#: SSE 轮询间隔
POLL_S = 1.0
#: SSE 最长存活（超过就断开，前端可重连轮询）
SSE_MAX_S = 900

#: 预算巡检间隔（秒）。1s 对「跑 60 次工具调用」这种分钟级任务足够细，
#: 又不会把 DB 压出无谓的查询；测试里可改成 0 做确定性断言。
BUDGET_POLL_S = 1.0

#: 进程内任务表（仅用于 cancel；重启后为空，靠心跳接管兜底）
_TASKS: dict[str, asyncio.Task] = {}

#: 并发上限：执行体在调 LLM 期间必须持有 DB 连接（见 run_copilot_question 的说明），
#: 所以这里必须限流，否则几个人同时深问就能把连接池吃干。
MAX_CONCURRENT = 3
_SLOTS: asyncio.Semaphore | None = None


def _slots() -> asyncio.Semaphore:
    global _SLOTS
    if _SLOTS is None:
        _SLOTS = asyncio.Semaphore(MAX_CONCURRENT)
    return _SLOTS


class SubmitRunBody(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    conversation_id: int | None = None
    recipe: str | None = Field(default=None, max_length=48)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ── 执行体（可被测试替换）────────────────────────────────────────────────
async def run_copilot_question(
    *, user_id: str, conversation_id: int | None, question: str, run_id: str | None = None
) -> int:
    """真正干活：建/复用会话并发问。返回 conversation_id。

    **单独成函数是为了可测**：单测替换它就不需要真 LLM，也不会污染配额。
    """
    from src.harness.trace import EXTERNAL_RUN_ID
    from src.web.api import chat as chat_api
    from src.db.models import ChatConversation, User
    from src.db.session import SessionLocal

    # ⚠️ 2026-10-25 生产事故：一开始这里在调用前就 db.close()，把**脱离 session 的 User**
    # 传给了 send_message，它一碰懒加载属性就 DetachedInstanceError（run 直接 failed）。
    # 只能把会话保持到调用结束 —— 代价是占用一个连接，所以外层有限流 MAX_CONCURRENT。
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            raise RuntimeError("用户不存在（可能是账号被删除）")
        if conversation_id is None:
            conv = ChatConversation(user_id=user.id, title=question[:20])
            db.add(conv)
            db.commit()
            db.refresh(conv)
            cid = conv.id
        else:
            cid = conversation_id

        body = chat_api.SendMessageBody(content=question)
        # ⚠️ 必须把已有 run_id 传进去复用：`_run_tool_loop` 自己会 start_copilot_run，
        # 不复用就会变成"一次执行两条轨迹"（一条空壳 + 一条真轨迹），
        # 指标全部重复计数（2026-10-25 生产实测）。
        token = EXTERNAL_RUN_ID.set(run_id) if run_id else None
        try:
            await chat_api.send_message(conversation_id=cid, body=body, user=user)
        finally:
            if token is not None:
                EXTERNAL_RUN_ID.reset(token)
        return cid
    finally:
        db.close()


@allow_cross_user  # 后台执行体按 run_id 写状态, 没有请求上下文; run 归属已在提交/查询时校验
def _mark(run_id: str, *, status: str, error: str | None = None,
          elapsed_ms: int | None = None, tool_calls: int | None = None,
          conversation_id: int | None = None) -> None:
    """状态迁移（每次用新 session —— 后台任务不能跨 await 复用一个 session）。"""
    from src.core import copilot_runtime as rt
    from src.db.models import CopilotRun
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        row = db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
        if row is None:
            return
        if not rt.can_transition(row.status, status):
            logger.warning("[copilot_jobs] 非法迁移 %s: %s → %s", run_id, row.status, status)
            return
        # 轨迹(trace)已经写过终态时不要覆盖它的真实数字（tool_calls/elapsed 是管线自己测的，
        # 我这里测的是整个 job 的墙钟时间，混着用会让统计口径不一致）
        if rt.is_terminal(row.status) and row.status == status:
            # 数字不覆盖，但**关联要补**：轨迹不知道前端会话 id，只有执行体知道。
            # 少了它，证据卡拿不到"这条 run 属于哪次对话"（2026-10-25 实测 conv=None）。
            changed = False
            if row.finished_at is None:
                row.finished_at = datetime.utcnow()
                changed = True
            if conversation_id is not None and row.conversation_id is None:
                row.conversation_id = conversation_id
                changed = True
            if changed:
                db.commit()
            return
        row.status = status
        if error:
            row.error = error[:2000]
        if elapsed_ms is not None:
            row.elapsed_ms = elapsed_ms
        if tool_calls is not None:
            row.tool_calls = tool_calls
        if conversation_id is not None:
            row.conversation_id = conversation_id
        if rt.is_terminal(status):
            row.finished_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


async def _heartbeat_loop(run_id: str, stop: asyncio.Event) -> None:
    from src.core import copilot_runtime as rt
    from src.db.session import SessionLocal

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_S)
            return
        except asyncio.TimeoutError:
            pass
        db = SessionLocal()
        try:
            rt.heartbeat(db, run_id)
        except Exception:
            logger.debug("[copilot_jobs] 心跳失败 %s", run_id, exc_info=True)
        finally:
            db.close()


def _audit_budget_breach(run_id: str, user_id: str, used: int, limit: int) -> None:
    """预算中止写审计（用独立 session —— 后台任务不能借用别人的会话）。"""
    from src.core import copilot_quota as quota
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        quota.audit_limit_hit(
            db, user_id=user_id, action=quota.AUDIT_TOOL_BUDGET,
            detail=f"run={run_id} used={used} limit={limit}",
        )
    finally:
        db.close()


async def _budget_watch(run_id: str, user_id: str, target: asyncio.Task):
    """盯着本 run 的工具调用数：超硬预算就 cancel 执行体，返回 `(原因, 已用, 上限)`。

    **为什么不在工具层拦**：`agents/chat/registry.charge_tool_budget` 的软预算只是"拒绝
    继续取数"，模型照样拿半截证据作答 —— 那正是「悄悄截断后继续」。硬预算必须在执行体
    外层才停得住整轮，所以这里读 `copilot_steps`（trace 每次调用落一行）并中止。
    正常结束返回 None；巡检本身出错也返回 None（fail-open，限流不该把任务打成失败）。
    """
    from src.core import copilot_quota as quota
    from src.db.session import SessionLocal

    while not target.done():
        db = SessionLocal()
        try:
            ok, used, limit = quota.check_run_budget(db, run_id)
        except Exception as e:  # noqa: BLE001 —— 见 docstring（fail-open）
            from src.core.db_safe import safe_rollback

            safe_rollback(db)
            logger.warning("[copilot_jobs] 预算巡检失败，本轮不拦 %s: %r", run_id, e)
            return None
        finally:
            db.close()
        if not ok:
            target.cancel()
            reason = (
                f"工具调用预算超限：已用 {used} 次 > 上限 {limit} 次，已中止本轮"
                f"（不截断后继续作答）"
            )
            logger.warning("[copilot_jobs] run %s 超预算中止: %s", run_id, reason)
            _audit_budget_breach(run_id, user_id, used, limit)
            return reason, used, limit
        done, _ = await asyncio.wait({target}, timeout=BUDGET_POLL_S)
        if target in done:
            return None
    return None


async def _execute(run_id: str, *, user_id: str, conversation_id: int | None, question: str) -> None:
    stop = asyncio.Event()
    hb = asyncio.create_task(_heartbeat_loop(run_id, stop))
    t0 = datetime.utcnow()
    _mark(run_id, status="running")
    try:
        async with _slots():  # 限流：同时最多 MAX_CONCURRENT 轮在跑
            inner = asyncio.create_task(asyncio.wait_for(
                run_copilot_question(
                    user_id=user_id, conversation_id=conversation_id,
                    question=question, run_id=run_id,
                ),
                timeout=RUN_TIMEOUT_S,
            ))
            watch = asyncio.create_task(_budget_watch(run_id, user_id, inner))
            try:
                inner_res, watch_res = await asyncio.gather(inner, watch, return_exceptions=True)
            finally:
                for t in (inner, watch):
                    if not t.done():
                        t.cancel()
        elapsed = int((datetime.utcnow() - t0).total_seconds() * 1000)
        if isinstance(watch_res, tuple):
            # 预算中止：审计已写，run 必须落 failed —— 不能因为"有半截答案"就写 ok
            reason, used, _limit = watch_res
            _mark(run_id, status="failed", elapsed_ms=elapsed, tool_calls=used, error=reason)
            return
        if isinstance(inner_res, asyncio.TimeoutError):
            _mark(run_id, status="failed", elapsed_ms=elapsed,
                  error=f"超时：超过 {RUN_TIMEOUT_S}s 未完成，已转后台失败（可重试）")
            return
        if isinstance(inner_res, asyncio.CancelledError):
            _mark(run_id, status="cancelled", error="用户取消")
            return
        if isinstance(inner_res, BaseException):
            logger.warning("[copilot_jobs] run %s 失败: %s", run_id, inner_res, exc_info=inner_res)
            _mark(run_id, status="failed", elapsed_ms=elapsed,
                  error=f"{type(inner_res).__name__}: {inner_res}")
            return
        _mark(run_id, status="ok", elapsed_ms=elapsed, conversation_id=inner_res)
    except asyncio.CancelledError:
        _mark(run_id, status="cancelled", error="用户取消")
        raise
    except Exception as e:  # noqa: BLE001 —— 任何执行失败都要落到 run 上，不能只写日志
        elapsed = int((datetime.utcnow() - t0).total_seconds() * 1000)
        logger.warning("[copilot_jobs] run %s 失败: %s", run_id, e, exc_info=True)
        _mark(run_id, status="failed", elapsed_ms=elapsed, error=f"{type(e).__name__}: {e}")
    finally:
        stop.set()
        hb.cancel()
        _TASKS.pop(run_id, None)


# ── 接口 ─────────────────────────────────────────────────────────────────
@router.post("/runs")
async def submit_run(body: SubmitRunBody, user=Depends(get_current_user)):
    """提交即返回 run_id —— 真正的异步，不在这里等 LLM。

    提交前先过**单用户并发上限**（P1-1.2）：超了 429 并写审计，不排队、不静默降级 ——
    排队会让用户以为在跑，实际等到超时更糟。
    """
    from src.core import copilot_quota as quota
    from src.db.models import CopilotRun
    from src.db.session import SessionLocal

    run_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        quota.enforce_run_concurrency(db, user)   # 超限: 内部已写审计并抛 429
        db.add(CopilotRun(
            run_id=run_id,
            conversation_id=body.conversation_id,
            user_id=user.id,
            recipe=body.recipe,
            question=body.question[:500],
            status="queued",
            tool_calls=0,
        ))
        db.commit()
    except Exception:
        from src.core.db_safe import safe_rollback

        safe_rollback(db)
        raise
    finally:
        db.close()

    task = asyncio.create_task(_execute(
        run_id, user_id=user.id, conversation_id=body.conversation_id, question=body.question,
    ))
    _TASKS[run_id] = task
    return {"run_id": run_id, "status": "queued", "events": f"/api/copilot/runs/{run_id}/events"}


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, user=Depends(get_current_user)):
    """SSE：状态迁移 + 工具步骤。终态自动关闭，不空转到超时。"""
    from src.core import copilot_runtime as rt
    from src.db.models import CopilotRun, CopilotStep
    from src.db.session import SessionLocal

    def _load():
        db = SessionLocal()
        try:
            row = db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
            if row is None:
                return None, []
            if row.user_id and row.user_id != user.id:
                return "forbidden", []
            steps = (
                db.query(CopilotStep)
                .filter(CopilotStep.run_id == run_id)
                .order_by(CopilotStep.seq.asc())
                .all()
            )
            return row, steps
        finally:
            db.close()

    row, _ = _load()
    if row is None:
        raise HTTPException(404, "run 不存在")
    if row == "forbidden":
        raise HTTPException(404, "run 不存在")  # 不暴露他人 run 的存在性

    async def gen():
        seen_steps = -1
        last_status = None
        elapsed = 0.0
        while elapsed < SSE_MAX_S:
            row, steps = _load()
            if row is None:
                yield _sse("error", {"message": "run 不存在"})
                return
            if row.status != last_status:
                last_status = row.status
                yield _sse("status", {
                    "status": row.status,
                    "elapsed_ms": row.elapsed_ms,
                    "error": row.error,
                    "tool_calls": row.tool_calls,
                    "conversation_id": row.conversation_id,
                })
            if len(steps) != seen_steps:
                seen_steps = len(steps)
                yield _sse("steps", {
                    "count": len(steps),
                    "items": [
                        {"seq": s.seq, "tool": s.tool, "ok": bool(s.ok),
                         "elapsed_ms": s.elapsed_ms}
                        for s in steps[:20]
                    ],
                })
            if rt.is_terminal(row.status):
                yield _sse("done", {"status": row.status, "error": row.error})
                return
            await asyncio.sleep(POLL_S)
            elapsed += POLL_S
        # 超时不断言失败 —— 只是停止推送，真实状态由心跳接管判定
        yield _sse("error", {"message": "事件流超时，请改用状态接口轮询"})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, user=Depends(get_current_user)):
    from src.db.models import CopilotRun
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        row = db.query(CopilotRun).filter(CopilotRun.run_id == run_id).first()
        if row is None or (row.user_id and row.user_id != user.id):
            raise HTTPException(404, "run 不存在")
        if row.status in ("ok", "degraded", "succeeded"):
            raise HTTPException(409, "该 run 已完成，无法取消")
    finally:
        db.close()

    task = _TASKS.get(run_id)
    if task and not task.done():
        task.cancel()          # _execute 里会写 cancelled
    else:
        _mark(run_id, status="cancelled", error="用户取消（任务已不在本进程，按中断处理）")
    return {"run_id": run_id, "status": "cancelled"}
