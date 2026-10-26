"""确定性管线（2026-10-21）：工具顺序/失败容忍/标的多只/证据拼装。"""
import asyncio

import pytest

from src.harness.pipeline import (
    PipelineRun,
    compose_prompt,
    run,
    session_context,
    symbols_of,
)


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


def test_names_fallback_when_no_code(monkeypatch):
    """只写名字（"全景诊断 三花智控"）也要能进管线，否则会退回 loop 并触发零工具拒绝。"""
    import src.harness.pipeline as pl

    monkeypatch.setattr(pl, "_names_to_codes", lambda t: ["002050"])
    assert pl.symbols_of("全景数智深度诊断 三花智控", None, allow_names=True) == ["002050"]
    assert pl.symbols_of("全景数智深度诊断 三花智控", None) == [], "默认不查名，避免无谓 TQ 调用"


def test_pipeline_records_steps_into_trace(monkeypatch):
    """管线里的工具调用必须进轨迹，否则治理页看不到、Jaccard 量不出来。"""
    import asyncio

    class Trace:
        def __init__(self):
            self.steps = []

        def step(self, tool, args=None, *, ok=True, error=None, result=None, elapsed_ms=None):
            self.steps.append((tool, ok, error))

    tr = Trace()

    async def ex(name, args):
        if name == "b":
            raise RuntimeError("x")
        return "data"

    asyncio.run(run(FakeRecipe(), ["002050"], ex, trace=tr))
    assert [s[0] for s in tr.steps] == ["a", "b", "c"]
    assert tr.steps[1][1] is False and tr.steps[0][1] is True


def test_answer_stream_yields_deltas_then_done():
    """生成要 25~135s，不流式用户只能干等——必须边生成边下发。"""
    import asyncio

    import src.harness.pipeline as pl

    class AI:
        async def chat_with_tools_stream(self, msgs, tools=None, temperature=0.4):
            yield "delta", "【结论】"
            yield "delta", "偏多"
            yield "final", type("M", (), {"content": "【结论】偏多"})()

    async def ex(name, args):
        return "现价 32.91"

    events = []

    async def go():
        async for k, v in pl.answer_stream(AI(), FakeRecipe(), "002050 怎么样", None, ex):
            events.append((k, v))

    asyncio.run(go())
    # v0.33.4: 取数阶段会先发 stage（每工具一条），正文事件顺序不变
    kinds = [k for k, _ in events if k != "stage"]
    assert kinds == ["delta", "delta", "done"]
    assert events[0][0] == "stage" and "正在取数" in events[0][1]
    assert events[-1][1].startswith("【结论】偏多")


# ── P0 管线架构修复（2026-10-22）：记录类工具不进取数 lane + 会话上下文不丢 ──


class RecordRecipe:
    """带记录类工具（fallback=False）的配方（模拟 deep_diagnosis）。"""

    id = "t_rec"
    tools = ("a", "note_new_dimension", "b")
    output_sections = ("结论", "口径")
    prompt_fragment = "你是测试员。"


def test_data_lane_never_calls_record_tools():
    """取数阶段绝不能盲调记录类工具：它要 dimension/reason，只给 symbol 必然失败并污染轨迹。

    v0.43.2 真机第 11 步那条 `note_new_dimension({"symbol": "600111"})` 就是管线留下的，
    不是模型调的（模型那时还没拿到工具）。
    """
    import asyncio

    calls = []

    async def ex(name, args):
        calls.append((name, dict(args)))
        return f"{name}:ok"

    runs, _ = asyncio.run(run(RecordRecipe(), ["002050"], ex))
    assert [c[0] for c in calls] == ["a", "b"], f"记录类工具被盲调了: {calls}"
    assert runs[0].ok == 2 and runs[0].failed == 0, "不该出现假失败"
    assert "note_new_dimension" not in dict(runs[0].outputs.keys() if hasattr(runs[0].outputs, "keys") else [])


def test_compose_prompt_keeps_session_context():
    """管线模式不能丢记忆/持仓/画像/视图：否则「我这只还能拿吗」不知道成本价。"""
    pr = PipelineRun(symbol="002050", outputs=[("a", "现价 32.91")], ok=1)
    system, _ = compose_prompt(
        FakeRecipe(), [pr], "002050 怎么样", context="【我的持仓】002050 成本 35.20"
    )
    assert "会话上下文" in system and "成本 35.20" in system


def test_compose_prompt_grants_record_tool_only_when_mounted():
    """挂了记录类工具才给它许可；没挂必须仍然写「没有调用工具的能力」。"""
    pr = PipelineRun(symbol="002050", outputs=[("a", "x")], ok=1)
    without, _ = compose_prompt(FakeRecipe(), [pr], "q")
    assert "没有调用工具的能力" in without
    with_post, _ = compose_prompt(RecordRecipe(), [pr], "q", post_tools=["note_new_dimension"])
    assert "note_new_dimension" in with_post
    assert "没有调用工具的能力" not in with_post


def test_answer_stream_executes_record_tool_call_from_model():
    """模型在结论轮调用记录类工具 → 必须真的执行（只写库，不影响正文）。"""
    import asyncio

    import src.harness.pipeline as pl

    seen_tools = []
    calls = []

    class _Fn:
        name = "note_new_dimension"
        arguments = '{"dimension": "股东户数变化", "reason": "只有十大股东"}'

    class _TC:
        function = _Fn()

    class _Final:
        content = "【结论】暂无该维度"
        tool_calls = [_TC()]

    class AI:
        async def chat_with_tools_stream(self, msgs, tools=None, temperature=0.4):
            seen_tools.append(list(tools or []))
            yield "delta", "【结论】暂无该维度"
            yield "final", _Final()

    async def ex(name, args):
        calls.append((name, dict(args)))
        return '{"created": true}'

    events = []

    async def go():
        async for k, v in pl.answer_stream(AI(), RecordRecipe(), "002050 怎么样", None, ex):
            events.append((k, v))

    asyncio.run(go())
    names = [c[0] for c in calls]
    assert "note_new_dimension" in names, f"模型点名的记录工具没被执行: {names}"
    rec = next(c for c in calls if c[0] == "note_new_dimension")
    assert rec[1]["dimension"] == "股东户数变化", "参数必须原样传下去"
    assert events[-1][0] == "done"


def test_answer_stream_hides_record_tools_when_not_mounted():
    """没挂记录类工具的配方，结论轮 tools 仍是空（既有行为不变）。"""
    import asyncio

    import src.harness.pipeline as pl

    seen = []

    class AI:
        async def chat_with_tools_stream(self, msgs, tools=None, temperature=0.4):
            seen.append(list(tools or []))
            yield "final", type("M", (), {"content": "ok", "tool_calls": []})()

    async def ex(name, args):
        return "data"

    async def go():
        async for _k, _v in pl.answer_stream(AI(), FakeRecipe(), "002050", None, ex):
            pass

    asyncio.run(go())
    assert seen == [[]], f"非记录类配额不该给工具: {seen}"


def test_session_context_strips_base_prompt_and_recipe_block():
    """会话上下文必须剥掉人设与配方块：人设里的「优先调用 dossier」与管线模式冲突。"""
    base = "你是数智分析BOT。"
    msgs = [{
        "role": "system",
        "content": base + "\n\n--- 当前数据 ---\n【持仓】成本 35.2"
        + "\n\n--- 本轮由用户指定的内置智能体：全景诊断（deep_diagnosis） ---\n配方正文",
    }]
    ctx = session_context(msgs, base)
    assert "成本 35.2" in ctx
    assert base not in ctx and "配方正文" not in ctx
    assert session_context([], base) == ""
    assert session_context(msgs, "别的人设") == "", "人设对不上时宁可没有上下文"


def test_propose_levels_only_fires_when_scenario_matrix_present(monkeypatch):
    """G4 结论上图：情景矩阵跑过才提议画线；没有矩阵/没有 db 时静默不动。

    P1 的 `actions.propose_scenario_annotations` 需要 db/user，只有 chat 层有 ——
    这里验证管线收尾这一枪打得准，且未命中时一次都不调。
    """
    from types import SimpleNamespace

    import src.harness.actions as actions
    import src.harness.pipeline as pl

    called = []
    monkeypatch.setattr(
        actions, "propose_scenario_annotations",
        lambda db, **kw: called.append(kw) or [],
    )
    user = SimpleNamespace(id=7)
    runs = [PipelineRun(symbol="002050", outputs=[("get_scenario_matrix", "阻力 35.8")], ok=1)]

    pl.propose_levels(FakeRecipe(), runs, db=object(), user=user)
    assert called and called[0]["symbol"] == "002050" and called[0]["user_id"] == 7

    called.clear()
    pl.propose_levels(FakeRecipe(), [PipelineRun(symbol="002050", outputs=[("a", "x")], ok=1)],
                      db=object(), user=user)
    assert called == [], "没有情景矩阵不该提议"

    pl.propose_levels(FakeRecipe(), runs, db=None, user=None)
    assert called == [], "没有 db/user 必须静默跳过"
