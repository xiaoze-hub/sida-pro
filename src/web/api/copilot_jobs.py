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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_

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


# ── 配方（recipe）：记录 ≡ 执行 ────────────────────────────────────────────
# 背景（2026-10-28 复核）：`SubmitRunBody.recipe` 原先只写进 `copilot_runs.recipe`，
# 执行链一次都不往下传 —— `_execute` 签名里没有 recipe，`run_copilot_question`
# 只构造 `SendMessageBody(content=question)`，没有 agent mention，于是
# `harness.context.apply_mentions` / `inject_recipe` 永远不触发：工具白名单、
# prompt_fragment、max_tool_calls 全部落空，而治理页 `by_recipe` 照旧计数。
# 也就是「记录说跑了配方、实际跑裸 chat」。两件事一起收口：
#   1. 入参必须能解析成**真实存在**的配方 id（未知值提交即拒绝，不落库不执行）；
#   2. 落库的值 == 实际生效的值（同一份归一后的 id 传给执行体），缺省仍然裸 chat。


def resolve_recipe_id(raw: str | None) -> str | None:
    """把入参 recipe 归一成真实存在的配方 id；未知值抛 400（提交即拒绝）。

    归一规则：去空白；空/纯空白视作「不指定配方」（None，保持默认路径）。
    返回的是 `Recipe.id` 而不是入参原文 —— 文件名与 YAML `id:` 可能不一致，
    只有 `Recipe.id` 才是 `apply_mentions` 真正能 load 到的那个键。
    """
    if raw is None:
        return None
    rid = raw.strip()
    if not rid:
        return None
    from src.harness.recipes import load_recipes

    recipes = load_recipes()
    rec = recipes.get(rid)
    if rec is None:
        known = "、".join(sorted(recipes)) or "（配方目录为空）"
        raise HTTPException(400, f"未知配方 {rid!r}：不存在的配方不能记账执行。可用：{known}")
    return rec.id


def _recorded_recipe(run_id: str | None, user_id: str | None) -> str | None:
    """读 run 行上记录的 recipe（配方的唯一事实源）。行缺失/**归属不符**/无值返回 None。

    **为什么必须 `run_id` + `user_id` 双重过滤**：行上的 recipe 不是备注，是**执行凭据**
    —— 工具白名单、`max_tool_calls`、prompt_fragment 全由它决定。只按 run_id 取，
    等于任何人拿到别人的 run_id 就能把自己这一轮套上别人的配方（自己的会话被别人的
    约束锁住，治理页 `by_recipe` 还会把这轮记到别人的配方名下）。

    归属不符与「这行没记录」同办（None，不抛错、不提示他人 run 的存在性）：
    这是读口径那条边界在执行侧的对应物。无主（NULL / 空串 / 空白）行同样不回落配方
    —— 审计读是**看历史**，借配方是**拿它去执行**，拿不准归属时宁可按裸 chat 跑。
    """
    if not run_id or not user_id:
        return None
    from src.db.models import CopilotRun
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        row = (
            db.query(CopilotRun)
            .filter(
                CopilotRun.run_id == run_id,
                CopilotRun.user_id == str(user_id),
            )
            .first()
        )
        raw = getattr(row, "recipe", None) if row is not None else None
    finally:
        db.close()
    return (str(raw).strip() or None) if raw is not None else None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _run_owner_id(row: Any) -> str | None:
    """取 run 的归属人 id；NULL / 空串 / 纯空白一律视作**无主**（返回 None）。

    空串与 NULL 同办：falsy 不等于「无主可放行」，脏数据不能变成后门。
    """
    raw = getattr(row, "user_id", None)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def _is_platform_owner(user: Any) -> bool:
    """平台 owner（审计角色）。严格精确匹配，不做大小写/前缀猜测 ——

    判不准 owner 时宁可**少放行**：无主数据的读权限是可审计的额外授权，
    不是默认权利，拼错的角色名（"Owner"/"owner "）不该把旧轨迹摊给一个普通账号。
    """
    return (getattr(user, "role", "") or "").strip() == "owner"


def run_read_scope_ok(row: Any, user: Any) -> bool:
    """**读**归属判定：有主 → 仅归属人；无主 → 仅平台 owner（审计只读）。

    **修的是什么**：这几处原写作 `row.user_id and row.user_id != user.id` ——
    `user_id` 为 NULL 时直接短路放行，等于任何登录账号拿到 run_id 就能读别人的
    历史轨迹（question/status/工具步骤）。

    **为什么会有 NULL**：`copilot_runs.user_id` 是 2026-10-20 才补的列
    （`src/web/migrations.py::_migrate_copilot_run_user_id`），之前落库的轨迹全是 NULL。
    这批合法旧数据**不回填归属**（不去猜是谁问的），只按 `role == "owner"`
    放给平台 owner 做审计读。

    ⚠️ 本函数**只许用于读**（详情/治理/并行/事件流）。写路径一律用
    `run_cancel_scope_ok` —— 无主的读权限不等于无主的写权限（见该函数说明）。
    """
    owner_id = _run_owner_id(row)
    if owner_id is None:
        return _is_platform_owner(user)
    return owner_id == str(getattr(user, "id", "") or "")


def run_user_scope_ok(row: Any, user: Any) -> bool:
    """历史名，等价于 `run_read_scope_ok`（`copilot_workbench` 两个只读端点仍在用）。

    保留只为不回改其他文件；**新代码不要用这个名字** —— 它读不出「读还是写」，
    正是这个歧义让 cancel 误用了读口径、owner 能把无主 run 取消掉。
    """
    return run_read_scope_ok(row, user)


def run_cancel_scope_ok(row: Any, user: Any) -> bool:
    """**写**归属判定（取消）：有主 → 仅归属人；**无主 → 一律拒绝（default deny）**。

    **为什么不能复用读口径**：读口径给 owner 开了「无主可审计读」，一旦被写路径复用，
    `POST /runs/{id}/cancel` 就等于让 owner 能改写无主 run 的状态 —— 那是**改历史**，
    不是看历史。读与写是两种授权，共用一个判据迟早会漏。

    **为什么不给 owner 也开无主写**：owner 角色权限本身无法在 run 行上被严格确认
    （角色来自当前会话，与这条旧 run 没有任何可验证的关联）。既然确认不了「谁有权
    动这条无主 run」，默认拒绝就是唯一诚实的答案；无主 run 若卡在 running，由
    `copilot_runtime.reap_stale_runs()` 按心跳收尸，不靠人来取消。
    """
    owner_id = _run_owner_id(row)
    if owner_id is None:
        return False
    return owner_id == str(getattr(user, "id", "") or "")


# ── 执行体（可被测试替换）────────────────────────────────────────────────
async def run_copilot_question(
    *, user_id: str, conversation_id: int | None, question: str,
    run_id: str | None = None, recipe: str | None = None,
) -> int:
    """真正干活：建/复用会话并发问。返回 conversation_id。

    **单独成函数是为了可测**：单测替换它就不需要真 LLM，也不会污染配额。

    **配方怎么生效**：把 recipe 转成 `Mention(type="agent")` 塞进 `SendMessageBody`
    —— 这是 chat.py 既有通路（`harness.context.apply_mentions` → `inject_recipe`
    → `route_for_request(forced=...)`），不需要改 chat.py、也不复制一份管线。
    显式入参优先；没传时回落到 run 行上记录的 recipe（`skills_gateway` 的任务入口
    只写行不传参，事实源必须是同一处，否则两条入口又会各说各话）。

    **未知配方不做静默降级**：行里记着一个已经不存在的配方时直接失败 ——
    「记录说跑了配方、实际裸 chat」正是本次要消掉的偏差，宁可 failed 也不许悄悄退化。
    """
    from src.harness.trace import EXTERNAL_RUN_ID
    from src.web.api import chat as chat_api
    from src.db.models import ChatConversation, User
    from src.db.session import SessionLocal

    # 归一 + 校验放在开库之前：未知配方不该建会话、不该烧配额
    recipe_id = (recipe or "").strip() or _recorded_recipe(run_id, user_id) or None
    if recipe_id is not None:
        from src.harness.recipes import get_recipe

        if get_recipe(recipe_id) is None:
            raise RuntimeError(
                f"配方 {recipe_id!r} 不存在（run {run_id or '-'} 记录了这个配方）；"
                "不允许退化成不带约束的裸 chat，请修正或清空 run.recipe"
            )

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

        if run_id:
            from src.web.api.copilot_message_link import bind_run_conversation
            if not bind_run_conversation(db, run_id, user.id, cid):
                raise RuntimeError("运行记录与会话归属无法核验")

        body_kwargs: dict[str, Any] = {"content": question}
        if recipe_id is not None:
            # 只带 agent mention：正文一字不改（用户原话是事实，配方是约束）
            from src.harness.context import Mention

            body_kwargs["mentions"] = [Mention(type="agent", value=recipe_id)]
        body = chat_api.SendMessageBody(**body_kwargs)
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


async def _execute(run_id: str, *, user_id: str, conversation_id: int | None, question: str,
                   recipe: str | None = None) -> None:
    """执行体。`recipe` 透传给执行函数；None 时由 `run_copilot_question` 回落到
    run 行上的记录（`skills_gateway` 的任务入口不传参，不能因此丢配方）。"""
    stop = asyncio.Event()
    hb = asyncio.create_task(_heartbeat_loop(run_id, stop))
    t0 = datetime.utcnow()
    _mark(run_id, status="running")
    try:
        async with _slots():  # 限流：同时最多 MAX_CONCURRENT 轮在跑
            inner = asyncio.create_task(asyncio.wait_for(
                run_copilot_question(
                    user_id=user_id, conversation_id=conversation_id,
                    question=question, run_id=run_id, recipe=recipe,
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
        # P2-2.2: run 进终态后投递外部回调（best-effort：投递失败不能影响 run 结果，
        # 也不能让异常冒到 asyncio 的 task 里变成"未取回异常"）
        try:
            from src.core import webhook_delivery as _wh
            from src.db.session import SessionLocal as _SL

            _wdb = _SL()
            try:
                _wh.deliver_for_run(_wdb, run_id)
            finally:
                _wdb.close()
        except Exception as _wexc:  # noqa: BLE001
            logger.warning("[copilot_jobs] webhook 投递异常(不影响 run): %s", _wexc)


# ── 接口 ─────────────────────────────────────────────────────────────────
@router.post("/runs")
async def submit_run(body: SubmitRunBody, user=Depends(get_current_user)):
    """提交即返回 run_id —— 真正的异步，不在这里等 LLM。

    提交前先过**单用户并发上限**（P1-1.2）：超了 429 并写审计，不排队、不静默降级 ——
    排队会让用户以为在跑，实际等到超时更糟。

    `recipe` 先归一成**真实存在**的配方 id 再落库（未知值 400，不写库不执行），
    并把同一个 id 传给执行体 —— 同一条 run 的「记录」与「实际生效」必须是同一个值。
    """
    from src.core import copilot_quota as quota
    from src.db.models import CopilotRun
    from src.db.session import SessionLocal

    # 未知配方在这里就拒（早于限流与落库）：400 且不留半条 run
    recipe_id = resolve_recipe_id(body.recipe)

    run_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        quota.enforce_run_concurrency(db, user)   # 超限: 内部已写审计并抛 429
        db.add(CopilotRun(
            run_id=run_id,
            conversation_id=body.conversation_id,
            user_id=user.id,
            recipe=recipe_id,
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
        run_id, user_id=user.id, conversation_id=body.conversation_id,
        question=body.question, recipe=recipe_id,
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
            # 归属**直接写进 where**，与 `run_read_scope_ok` 同一条边界：
            #   本人；或（仅平台 owner）无主行（NULL / 空白，审计只读）。
            # 为什么必须落在 SQL 上：只把整行取回内存再判归属时，越权行的
            # question / status / 工具步骤已经进了进程，缺的只是一次 `if` ——
            # 任何把 `_load()` 返回值当成"已经过关"的后来者都会把它漏出去。
            # 口径与 `_run_owner_id` 对齐（NULL / 空串 / 空白都算无主）。
            # 归属条件就写在这条 filter 里（不是先算好再传变量）：拿掉它 = 查询
            # 里再也没有 user_id，`check_scoped_queries.py` 会立刻变红。
            uid = str(getattr(user, "id", "") or "")
            row = (
                db.query(CopilotRun)
                .filter(
                    CopilotRun.run_id == run_id,
                    (
                        or_(
                            CopilotRun.user_id == uid,
                            CopilotRun.user_id.is_(None),
                            func.trim(CopilotRun.user_id) == "",
                        )
                        if _is_platform_owner(user)
                        else CopilotRun.user_id == uid
                    ),
                )
                .first()
            )
            if row is None:
                return None, []
            # 第二道：SQL 被谁改宽 / 判据漂移时在这里立刻拦住，不靠"取到了就信"。
            if not run_read_scope_ok(row, user):
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
    """取消 = **写**：只认 run 的归属人，无主 run 一律拒绝（`run_cancel_scope_ok`）。

    这里刻意**不用**读口径 `run_read_scope_ok` —— 读口径给 owner 开了无主审计读，
    复用到写路径就等于 owner 能改写无主旧 run 的状态。判不准谁有权动无主 run 时
    默认拒绝；卡住的无主 run 交给 `copilot_runtime.reap_stale_runs()` 按心跳收尸。
    """
    from src.db.models import CopilotRun
    from src.db.session import SessionLocal

    db = SessionLocal()
    try:
        # 写口径在 SQL 里就是「只认本人」：他人的行 / 无主的行都取不到，
        # default deny 不靠事后判断兜底（`run_cancel_scope_ok` 是第二道断言）。
        row = (
            db.query(CopilotRun)
            .filter(
                CopilotRun.run_id == run_id,
                CopilotRun.user_id == str(getattr(user, "id", "") or ""),
            )
            .first()
        )
        if row is None or not run_cancel_scope_ok(row, user):
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
