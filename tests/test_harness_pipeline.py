"""确定性管线（2026-10-21）：工具顺序/失败容忍/标的多只/证据拼装。"""
import asyncio

import pytest

from src.harness.pipeline import PipelineRun, compose_prompt, run, symbols_of


class FakeRecipe:
    id = "t"
    tools = ("a", "b", "c")
    output_sections = ("结论", "口径")
    prompt_fragment = "你是测试员。"


def test_symbols_prefers_explicit_codes():
    assert symbols_of("002050 和 301128 怎么看", bound="600111") == ["002050", "301128"]
    assert symbols_of("它怎么样", bound="600111") == ["600111"]
    assert symbols_of("它怎么样") == []


def test_symbols_cap_two():
    assert len(symbols_of("002050 301128 600111 002230")) == 2, "多标的只跑前两只，别把管线拖死"


def test_run_collects_outputs_in_order():
    async def ex(name, args):
        return f"{name}:{args['symbol']}"

    runs, errs = asyncio.run(run(FakeRecipe(), ["002050"], ex))
    assert errs == []
    assert [t for t, _ in runs[0].outputs] == ["a", "b", "c"]
    assert runs[0].ok == 3 and runs[0].failed == 0


def test_run_tolerates_single_tool_failure():
    async def ex(name, args):
        if name == "b":
            raise RuntimeError("boom")
        return "ok"

    runs, errs = asyncio.run(run(FakeRecipe(), ["002050"], ex))
    assert runs[0].ok == 2 and runs[0].failed == 1
    assert any("b:" in e for e in errs), "失败要留痕，别静默"


def test_run_marks_all_failed_as_unusable():
    async def ex(name, args):
        raise RuntimeError("down")

    runs, _ = asyncio.run(run(FakeRecipe(), ["002050"], ex))
    assert runs[0].usable is False, "全失败必须能识别，调用方要回落到 loop"


def test_compose_prompt_embeds_evidence_and_forbids_invention():
    pr = PipelineRun(symbol="002050", outputs=[("a", "现价 32.91")], ok=1)
    system, user = compose_prompt(FakeRecipe(), [pr], "002050 怎么样")
    assert "现价 32.91" in user and "002050 怎么样" in user
    assert "缺失的维度写「暂无」" in system
    assert "没有调用工具的能力" in system, "必须让模型知道它不能自己取数"
    assert "结论 / 口径" in system


def test_compose_prompt_flags_empty_symbol():
    pr = PipelineRun(symbol="002050")
    _, user = compose_prompt(FakeRecipe(), [pr], "002050")
    assert "数据未取到" in user
