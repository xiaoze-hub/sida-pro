"""`number_audit.unverified_items` → `chat_messages.evidence_audit.unverified` 的桥接。

为什么值得单独钉：
这不是一个字段搬运问题，而是**两处早退**曾经让整条链空转：
1. `take()` 原来只认「行级 rows[].unverified」，整段级数字问题会被整包丢掉；
2. `_stage()` 原来在没有【依据】段时直接 return —— 而管线配方输出的是
   【结论】【前期走势】【筹码与资金】…，根本没有【依据】段。
合起来的后果：最常见的「模型编了个数字」永远到不了前端，UI 永远安静。
"""

from __future__ import annotations

import json

from src.web.api.copilot_message_save import record_with_audit, take

PIPELINE_ANSWER = """【结论】一句话：主力净流出 1.2 亿元，价格跌破 32.80 支撑。
【前期走势】近 5 日跌幅 3.2%，20 日动量 -3.88%，处于 55 分位。
【口径】tick 逐笔口径，时点 14:30:05。
"""


def _audit_with_unverified() -> dict:
    """形状与 src/core/number_audit.audit() 一致（只取本测试关心的键）。"""
    return {
        "checked": 3,
        "unverified": ["32.80"],
        "unverified_items": [
            {"value": "32.80", "kind": "price", "context": "价格跌破 32.80 支撑"},
        ],
        "magnitude": [],
        "ratio": 0.66,
        "severe": False,
    }


def test_pipeline_answer_without_basis_section_still_carries_unverified():
    """没有【依据】段（管线配方）也必须把未对账数字带到前端。"""
    text = PIPELINE_ANSWER
    record_with_audit(text, _audit_with_unverified())
    raw = take(text)
    assert raw is not None, "没有【依据】段时整包被丢弃 —— 前端永远看不到未对账数字"
    payload = json.loads(raw)
    assert payload["rows"] == []
    assert [x["value"] for x in payload["unverified"]] == ["32.80"]
    assert payload["unverified"][0]["kind"] == "price"


def test_clean_answer_is_not_staged():
    """没有问题的回答不要留噪音包（NULL 语义 = 未计算，不是零问题）。"""
    clean = {"checked": 2, "unverified": [], "unverified_items": [], "magnitude": [], "ratio": 1.0, "severe": False}
    record_with_audit(PIPELINE_ANSWER, clean)
    assert take(PIPELINE_ANSWER) is None


def test_fingerprint_mismatch_discards():
    """正文被替换/断流半截时不许把对账贴到不相干的正文上。"""
    text = PIPELINE_ANSWER
    record_with_audit(text, _audit_with_unverified())
    assert take("完全不同的另一半正文") is None


def test_payload_is_json_serializable():
    record_with_audit(PIPELINE_ANSWER, _audit_with_unverified())
    raw = take(PIPELINE_ANSWER)
    assert raw is not None
    json.loads(raw)  # 不抛 = 可序列化


def test_take_clears_pending():
    """取一次即清空，避免上一轮的对账挂到下一轮正文上。"""
    record_with_audit(PIPELINE_ANSWER, _audit_with_unverified())
    assert take(PIPELINE_ANSWER) is not None
    assert take(PIPELINE_ANSWER) is None


def test_emit_uses_context_sink_when_no_explicit_sink():
    """没显式传 sink 时必须落到当前请求的 ContextVar sink（chat.py 就靠这条路）。"""
    from src.core.evidence_pack import CURRENT_SINK, emit
    from src.harness.pipeline import PipelineRun

    r = PipelineRun(symbol="000001", outputs=[("tool_a", "data")], ok=1)
    sink: list = []
    token = CURRENT_SINK.set(sink)
    try:
        emit([r], "run-ctx", as_of="2026-11-03 15:00")
    finally:
        CURRENT_SINK.reset(token)
    assert len(sink) == 1 and sink[0]["evidence_id"] == "run-ctx:1"


def test_emit_is_noop_without_sink():
    """普通对话（没设置 sink）不许炸，也不许往外冒东西。"""
    from src.core.evidence_pack import CURRENT_SINK, emit
    from src.harness.pipeline import PipelineRun

    assert CURRENT_SINK.get() is None
    emit([PipelineRun(symbol="000001", outputs=[("t", "x")], ok=1)], "run-x")  # 不抛 = 通过


def test_async_generator_contextvar_is_visible_to_caller():
    """硬钉一个底层假设：chat.py 在 `_run_tool_loop`(async generator) 里 set 的 sink，
    端点（调用方）迭代完之后必须能读到。这条一旦不成立，证据永远是空的 —— 
    而界面看起来完全正常（只是没有证据区），属于最难发现的那种空转。"""
    import asyncio

    from src.core.evidence_pack import CURRENT_SINK

    async def gen():
        CURRENT_SINK.set(["filled-by-generator"])
        yield "x"

    async def caller():
        async for _ in gen():
            pass
        return CURRENT_SINK.get()

    assert asyncio.run(caller()) == ["filled-by-generator"]
