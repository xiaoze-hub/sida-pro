"""数智参谋的执行轨迹(Agent 内核 P0, 2026-10-20)。

问题: 参谋现在只留「回答文本」。出了「竞价被别名成 strongest」这类事故, 只能翻日志猜;
配方/提示词改好改坏也没有回放依据, 评测全靠人工抽检 18 次。

做法: 每次问答一行 `copilot_runs`, 每步工具调用一行 `copilot_steps`。
两处刻意的克制:
- `result_digest` 只存前 500 字 —— 工具返回里有用户持仓/自选, 全量落库是二次留存;
- 所有写入都吞异常 —— 轨迹是旁路, 绝不能因为记录失败把用户的问题搞挂。

读取侧(回放 UI / 评测集)见 `docs/design/agent-kernel.md` P2。
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

import logging

from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

MAX_ARG_CHARS = 2000
MAX_DIGEST_CHARS = 500
MAX_QUESTION_CHARS = 500


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    s = str(text)
    return s if len(s) <= limit else s[:limit] + "…"


class CopilotTrace:
    """一次问答的轨迹句柄；写库失败不影响主链路。"""

    def __init__(
        self,
        *,
        conversation_id: int | None = None,
        user_id: int | None = None,
        recipe: str | None = None,
        question: str | None = None,
    ) -> None:
        self.run_id = str(uuid.uuid4())
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.recipe = recipe
        self.question = _clip(question, MAX_QUESTION_CHARS)
        self.sequence = 0
        self.tool_calls = 0
        # 内存里也留一份步骤摘要 —— finish 时据此推断 ok/degraded/error。
        # 不查库的原因: 轨迹写库本身可能失败(旁路), 推断不能依赖它成功。
        self.steps: list[dict] = []
        self._started = time.perf_counter()

    # -- 写入 ---------------------------------------------------------------
    def open(self) -> None:
        """落 running 行。失败不抛。"""
        try:
            from src.db.models import CopilotRun

            db = SessionLocal()
            try:
                db.add(CopilotRun(
                    run_id=self.run_id,
                    conversation_id=self.conversation_id,
                    user_id=self.user_id,
                    recipe=self.recipe,
                    question=self.question,
                    status="running",
                    created_at=datetime.utcnow(),
                ))
                db.commit()
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 - 轨迹是旁路, 但必须留痕
            # 2026-10-20 教训: 完全静默会让「表建好了却没数据」无从查起(生产实测踩过)
            logger.warning("[trace] open 写库失败: %s: %s", type(e).__name__, e)

    def step(
        self,
        tool: str,
        args: dict | None = None,
        *,
        ok: bool = True,
        error: str | None = None,
        result: str | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        """记一步工具调用。失败不抛。"""
        self.sequence += 1
        self.tool_calls += 1
        self.steps.append({"tool": str(tool), "ok": bool(ok), "error": error})
        try:
            from src.db.models import CopilotStep

            db = SessionLocal()
            try:
                db.add(CopilotStep(
                    run_id=self.run_id,
                    seq=self.sequence,
                    tool=str(tool)[:64],
                    args=_clip(json.dumps(args or {}, ensure_ascii=False, default=str), MAX_ARG_CHARS),
                    ok=bool(ok),
                    error=_clip(error, 500),
                    result_digest=_clip(result, MAX_DIGEST_CHARS),
                    elapsed_ms=elapsed_ms,
                    created_at=datetime.utcnow(),
                ))
                db.commit()
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 - 旁路, 但必须留痕
            logger.warning("[trace] 写库失败: %s: %s", type(e).__name__, e)

    def set_recipe(self, recipe_id: str | None) -> None:
        """路由确定后回写配方 id（内存 + 已落库的 running 行）。失败不抛。"""
        self.recipe = _clip(recipe_id, 48) if recipe_id is not None else None
        try:
            from src.db.models import CopilotRun

            db = SessionLocal()
            try:
                row = db.query(CopilotRun).filter(CopilotRun.run_id == self.run_id).one_or_none()
                if row is not None:
                    row.recipe = self.recipe
                    db.commit()
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 - 旁路, 但必须留痕
            logger.warning("[trace] 写库失败: %s: %s", type(e).__name__, e)

    # -- 状态推断 -----------------------------------------------------------
    def _infer_status(self, status: str, error: str | None) -> tuple[str, str | None]:
        """把「工具全败/部分失败」从 ok 里拆出来, 让治理页看得到失败率。

        规则(调用方显式传非 ok 时不覆盖):
        - 无工具调用 → ok;
        - 全失败 → error;
        - 部分失败 → degraded;
        - 全成功 → ok。
        并把 failed_tools 计数与首个错误写进 error(已有错误保留在前)。
        """
        steps = list(getattr(self, "steps", []) or [])
        total = len(steps)
        failed = [s for s in steps if not s.get("ok")]
        n_failed = len(failed)

        detail: str | None = None
        if n_failed:
            first = failed[0]
            first_err = first.get("error")
            detail = f"failed_tools={n_failed}/{total}"
            if first_err:
                detail += f"; first_error={first.get('tool')}: {first_err}"
            else:
                detail += f"; first_error_tool={first.get('tool')}"

        merged = error
        if detail and detail not in (merged or ""):
            merged = f"{merged} | {detail}" if merged else detail

        if status != "ok":
            return status, merged          # 调用方显式状态优先(timeout/error/degraded)
        if total == 0:
            return ("error" if error else "ok"), merged
        if n_failed == total:
            return "error", merged
        if n_failed:
            return "degraded", merged
        return "ok", merged

    def finish(self, *, status: str = "ok", error: str | None = None) -> None:
        """收尾。ok/error 之外还可能是 degraded（部分工具失败）。"""
        status, error = self._infer_status(status, error)
        elapsed_ms = int((time.perf_counter() - self._started) * 1000)
        try:
            from src.db.models import CopilotRun

            db = SessionLocal()
            try:
                row = db.query(CopilotRun).filter(CopilotRun.run_id == self.run_id).one_or_none()
                if row is None:
                    db.add(CopilotRun(
                        run_id=self.run_id,
                        conversation_id=self.conversation_id,
                        user_id=self.user_id,
                        recipe=self.recipe,
                        question=self.question,
                        status=status,
                        error=_clip(error, 500),
                        tool_calls=self.tool_calls,
                        elapsed_ms=elapsed_ms,
                        created_at=datetime.utcnow(),
                        finished_at=datetime.utcnow(),
                    ))
                else:
                    row.status = status
                    row.recipe = self.recipe
                    row.error = _clip(error, 500)
                    row.tool_calls = self.tool_calls
                    row.elapsed_ms = elapsed_ms
                    row.finished_at =            datetime.now(timezone.utc)
                db.commit()
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 - 旁路, 但必须留痕
            logger.warning("[trace] 写库失败: %s: %s", type(e).__name__, e)


def start_copilot_run(
    *,
    conversation_id: int | None = None,
    user_id: int | None = None,
    recipe: str | None = None,
    question: str | None = None,
) -> CopilotTrace | None:
    """开一条轨迹。任何异常都返回 None，调用方按"没有轨迹"继续跑。"""
    try:
        trace = CopilotTrace(
            conversation_id=conversation_id,
            user_id=user_id,
            recipe=recipe,
            question=question,
        )
        trace.open()
        return trace
    except Exception:  # noqa: BLE001
        return None


def set_recipe(trace: CopilotTrace | None, recipe_id: str | None) -> None:
    """路由确定后把本轮命中的配方 id 回写进轨迹(内存 + 已落库的 running 行)。

    单列成模块函数而不是只留方法: 调用点(chat.py)在路由之后手里是
    `trace | None`, 这里统一吞掉 None 与写失败 —— 轨迹是旁路, 不能因为
    「记配方」把用户的问题搞挂。
    """
    if trace is None:
        return
    try:
        trace.set_recipe(recipe_id)
    except Exception as e:  # noqa: BLE001 - 旁路, 但必须留痕
        logger.warning("[trace] set_recipe 失败(忽略): %s: %s", type(e).__name__, e)


def finish_copilot_run(trace: CopilotTrace | None, *, status: str = "ok", error: str | None = None) -> None:
    """收尾并把状态分类: 全败 error / 部分失败 degraded / 全成 ok。

    调用方只交代「自己知道的」状态; 工具层的成败由 trace.steps 说话。
    显式传了非 ok(timeout/error/degraded)的调用方不覆盖 —— 那通常是更上游、
    更准确的判断, 不该被工具成败降级掉。
    """
    if trace is None:
        return
    try:
        # 与 finish() 内部那次推断等价且幂等(status 已非 ok 直接早返回,
        # error 里已有的 detail 不会重复追加), 这里显式做一次是为了让
        # 「按 steps 推断」这个契约落在对外入口上, 调用方不必知道 finish 的实现。
        status, error = trace._infer_status(status, error)
        trace.finish(status=status, error=error)
    except Exception:  # noqa: BLE001
        pass


def bind_llm_context(user, trace) -> None:
    """把本轮 user_id/run_id 绑到 contextvar，供 ai_client 写 llm_usage 时归因（P3）。

    吞掉所有异常：归因失败绝不能影响问答。不 unbind —— 请求任务结束上下文即销毁。
    """
    try:
        from src.core.llm_context import bind

        bind(
            user_id=getattr(user, "id", None),
            run_id=(getattr(trace, "run_id", None) if trace is not None else None),
            scene="chat",
        )
    except Exception:  # noqa: BLE001
        logger.debug("LLM 归因绑定失败(忽略)", exc_info=True)
