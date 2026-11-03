"""测试结构化证据对象与管道提示词注入（2026-11-03）。"""

import asyncio
import pytest
from dataclasses import dataclass, field

from src.core.evidence_pack import EvidenceRef, build_refs, to_payload
from src.harness.pipeline import PipelineRun, compose_prompt, answer, answer_stream


def test_evidence_pack_basic():
    # 测试 ok / missing / digest 截断 / evidence_id 唯一性
    r1 = PipelineRun(symbol="000001", outputs=[("tool_a", "short text"), ("tool_b", "a" * 300)], ok=2, failed=0)
    r2 = PipelineRun(symbol="000002", outputs=[("tool_c", "")], ok=0, failed=1)

    refs = build_refs([r1, r2], run_id="run-123", as_of="2026-11-03 15:00")
    assert len(refs) == 3

    assert refs[0].evidence_id == "run-123:1"
    assert refs[0].status == "ok"
    assert refs[0].digest == "short text"
    assert refs[0].run_id == "run-123"
    assert refs[0].as_of == "2026-11-03 15:00"

    assert refs[1].evidence_id == "run-123:2"
    assert refs[1].status == "ok"
    assert len(refs[1].digest) == 201  # 200 + '…'
    assert refs[1].digest.endswith("…")

    assert refs[2].evidence_id == "run-123:3"
    assert refs[2].status == "missing"
    assert refs[2].digest == ""

    payload = to_payload(refs)
    assert isinstance(payload, list)
    assert len(payload) == 3
    assert payload[0]["evidence_id"] == "run-123:1"
    assert payload[0]["tool"] == "tool_a"


class MockRecipe:
    output_sections = ["结论", "明细"]
    prompt_fragment = "测试指令"
    tools = ["tool_a"]


def test_compose_prompt_fact_pack():
    recipe = MockRecipe()
    r = PipelineRun(symbol="000001", outputs=[("tool_a", "data 123")], ok=1)
    
    # 不传 fact_pack 时
    sys1, user1 = compose_prompt(recipe, [r], "请问平安银行", context="", post_tools=())
    
    # 传 fact_pack 时
    fact_pack = {
        "pack_id": "fp-1",
        "as_of": "2026-11-03 15:00",
        "source": "TDX-L2",
        "text": "【事实包正文内容】"
    }
    sys2, user2 = compose_prompt(recipe, [r], "请问平安银行", context="", post_tools=(), fact_pack=fact_pack)

    assert "--- 本轮事实包（冻结于 2026-11-03 15:00，来源 TDX-L2）---" in sys2
    assert "【事实包正文内容】" in sys2
    assert "任何结论不得引用晚于该时点的数据；缺项写「暂无」" in sys2

    # 验证不传 fact_pack 时与改前逐字符一致（没有事实包注入）
    assert "--- 本轮事实包" not in sys1
    assert sys1 == (
        "测试指令\n\n以下是本轮**已经取到**的全部数据（由系统按固定顺序调用，你没有调用工具的能力）。"
        "请只依据这些数据写结论：每个数字必须能在下面的证据里找到原文，缺失的维度写「暂无」，"
        "不要补充任何未列出的数据，也不要推测。\n输出段落：结论 / 明细"
        "\n\n【通用排版铁律 —— 禁止教科书式长段落】\n1) 第一段必须先给一句话结论（≤45 字，带方向/状态 + 两个关键数字）。\n2) 每个段落下的内容一律写「- 」要点列表或 GFM 表格；每条要点独立成行、≤50 字、以数字或标的开头。\n3) 任何地方禁止出现超过 3 行的连续散文段落；禁止使用「从盘面看/整体而言/值得注意的是/综上所述」等套话。\n4) 段落标题单独成行（用【段落名】包起来），不许用 markdown `#` 标题."
    )


class _FakeTrace:
    run_id = "run-abc"


def test_emit_evidence_writes_serializable_dicts():
    """sink 里的东西会原样进 SSE done 载荷 —— 必须是 dict，dataclass 会在 json.dumps 炸掉。"""
    import json

    from src.harness.pipeline import _emit_evidence

    r = PipelineRun(symbol="000001", outputs=[("tool_a", "data 123")], ok=1)
    sink: list = []
    _emit_evidence([r], _FakeTrace(), {"as_of": "2026-11-03 15:00"}, sink)

    assert len(sink) == 1
    assert isinstance(sink[0], dict), "sink 必须是可序列化 dict"
    assert sink[0]["evidence_id"] == "run-abc:1"
    assert sink[0]["as_of"] == "2026-11-03 15:00"
    # 真序列化一次，这是 SSE 实际会做的事
    assert json.dumps(sink, ensure_ascii=False)


def test_emit_evidence_never_raises():
    """证据是旁路：任何构建失败都不许把整轮回答搞炸。"""

    class _Broken:
        @property
        def outputs(self):
            raise RuntimeError("boom")

    from src.harness.pipeline import _emit_evidence

    sink: list = []
    _emit_evidence([_Broken()], _FakeTrace(), None, sink)   # 不抛 = 通过
    assert sink == []


def test_emit_evidence_no_sink_is_noop():
    from src.harness.pipeline import _emit_evidence

    r = PipelineRun(symbol="000001", outputs=[("tool_a", "x")], ok=1)
    _emit_evidence([r], _FakeTrace(), None, None)  # 不传 sink 不报错
