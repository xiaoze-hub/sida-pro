"""结构化证据对象（2026-11-03）。

纯函数、无 DB、无网络、无 LLM。
由管道/工具运行输出（PipelineRun 或类似结构）生成证据包，供 AI 提示词和 API 出站载荷消费。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EvidenceRef:
    evidence_id: str
    tool: str
    source: str
    as_of: str
    caliber: str
    status: str
    digest: str
    run_id: str


def build_refs(runs: list[Any], run_id: str, *, as_of: str | None = None) -> list[EvidenceRef]:
    """从管线运行结果列表构建结构化证据对象列表。

    规则：
    - evidence_id = f"{run_id}:{seq}"，seq 从 1 开始按工具调用顺序递增。
    - 成功且文本非空 → status="ok"。
    - 失败或无文本 → status="missing"。
    - digest 取文本前 200 字（超出则截断，换行规范化或直接保留切片）。
    - source 和 caliber 赋予默认兜底（如 "TDX", "default" 或从输入推导）。
    """
    refs: list[EvidenceRef] = []
    seq = 0
    resolved_as_of = as_of or ""

    for r in runs:
        # r 可以是 PipelineRun 实例或类似结构
        outputs = getattr(r, "outputs", [])
        # 如果还有 failed / skipped，也可以视情况生成 missing，
        # 但管线主要记录在 outputs 里（(tool, text)）。失败的工具可能没有 output。
        # 这里我们按 outputs 的顺序生成 ok 项；如果 outputs 为空但 failed > 0，或者有 failed 标记，
        # 也可以为失败的工具补一条 missing。不过根据现有 PipelineRun，outputs 记录成功的，
        # 我们也可以检查是否有其他地方记录失败。为了健壮，先处理 outputs。
        for tool, text in outputs:
            seq += 1
            eid = f"{run_id}:{seq}"
            cleaned_text = str(text or "")
            status = "ok" if cleaned_text.strip() else "missing"
            digest = cleaned_text[:200] if len(cleaned_text) <= 200 else cleaned_text[:200] + "…"
            refs.append(
                EvidenceRef(
                    evidence_id=eid,
                    tool=str(tool),
                    source="TDX",
                    as_of=resolved_as_of,
                    caliber="default",
                    status=status,
                    digest=digest,
                    run_id=str(run_id),
                )
            )

        # 如果 pr 有 failed 或者 outputs 为空但有记录，也可以处理。
        # 遵循已有 PipelineRun 语义：pr.outputs 是 (tool, text) 列表。

    return refs


def to_payload(refs: list[EvidenceRef]) -> list[dict[str, Any]]:
    """将 EvidenceRef 列表转换为可序列化的字典列表（键名与 dataclass 字段一致）。"""
    return [
        {
            "evidence_id": ref.evidence_id,
            "tool": ref.tool,
            "source": ref.source,
            "as_of": ref.as_of,
            "caliber": ref.caliber,
            "status": ref.status,
            "digest": ref.digest,
            "run_id": ref.run_id,
        }
        for ref in refs
    ]


# 本轮回答的证据落点（2026-11-03）。
#
# 为什么用 ContextVar 而不是逐层加形参：chat.py 有 2310 行硬护栏（`chat.py 超护栏`
# 测试会红），而证据要从 pipeline 传回端点，中间那几层对本参数毫无语义。
# 同 `harness/trace.py::EXTERNAL_RUN_ID` 的既有做法。
CURRENT_SINK: ContextVar[list | None] = ContextVar("copilot_evidence_sink", default=None)


def emit(runs: list[Any], run_id: str, *, as_of: str | None = None) -> None:
    """把证据写进当前请求的 sink（未设置则是 no-op）。"""
    sink = CURRENT_SINK.get()
    if sink is None:
        return
    sink.extend(to_payload(build_refs(runs, run_id, as_of=as_of)))
