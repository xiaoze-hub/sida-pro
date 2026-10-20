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
from datetime import datetime

from src.db.session import SessionLocal

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
        except Exception:  # noqa: BLE001 - 轨迹是旁路
            pass

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
        except Exception:  # noqa: BLE001
            pass

    def finish(self, *, status: str = "ok", error: str | None = None) -> None:
        """收尾。状态只能是 ok/error；elapsed_ms 由 start 时刻算出。"""
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
                    row.error = _clip(error, 500)
                    row.tool_calls = self.tool_calls
                    row.elapsed_ms = elapsed_ms
                    row.finished_at = datetime.utcnow()
                db.commit()
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            pass


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


def finish_copilot_run(trace: CopilotTrace | None, *, status: str = "ok", error: str | None = None) -> None:
    if trace is None:
        return
    try:
        trace.finish(status=status, error=error)
    except Exception:  # noqa: BLE001
        pass
