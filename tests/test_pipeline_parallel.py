"""确定性管线（2026-10-21 第二批）：单标的内有界并发 + TQ 串行 lane。

这些断言守的是「并发不许改语义」：
- 顺序：输出 / trace / errors 一律按 `recipe.tools` 顺序，与谁先跑完无关；
- 有界：普通工具峰值 ≤ PIPELINE_CONCURRENCY，TQ 类峰值 == 1（TQ 单线程，连打会崩 TdxW.exe）；
- 隔离：单个工具异常/超时/空文本只影响它自己，其余全部照跑。
"""
import asyncio
import time

import pytest

import src.harness.pipeline as pl
from src.harness.pipeline import run, run_stream


class Recipe:
    def __init__(self, tools, output_sections=("结论",), prompt_fragment="你是测试员。"):
        self.id = "t"
        self.tools = tuple(tools)
        self.output_sections = tuple(output_sections)
        self.prompt_fragment = prompt_fragment


class Counter:
    """并发探针：记录当前在跑数量与历史峰值，可按工具名分组统计。"""

    def __init__(self, match=None):
        self.now = 0
        self.max = 0
        self.match = match  # 只统计名字满足条件的工具

    def _hit(self, name):
        return self.match is None or self.match(name)

    def __enter__(self):
        self.now += 1
        self.max = max(self.max, self.now)

    def __exit__(self, *exc):
        self.now -= 1


ORDER_TOOLS = [f"tool_{i}" for i in range(6)]


def test_parallel_speed_order_and_peak(monkeypatch):
    """6 个 0.2s 工具：串行 1.2s，并发 3 应 ≈0.4s；顺序仍按配方。"""
    peak = Counter()

    async def ex(name, args):
        with peak:
            await asyncio.sleep(0.2)
        return f"{name}:{args['symbol']}"

    t0 = time.perf_counter()
    runs, errs = asyncio.run(run(Recipe(ORDER_TOOLS), ["002050"], ex))
    elapsed = time.perf_counter() - t0

    assert errs == []
    assert elapsed < 0.8, f"并发没生效？串行会是 1.2s，实测 {elapsed:.3f}s"
    assert [t for t, _ in runs[0].outputs] == ORDER_TOOLS, "输出顺序必须等于 recipe.tools 顺序"
    assert runs[0].ok == 6 and runs[0].failed == 0
    assert peak.max <= pl.PIPELINE_CONCURRENCY, f"并发峰值 {peak.max} 超过上限"
    assert peak.max >= 2, "必须真的并行（只有 1 说明退化成串行）"


def test_tq_tools_run_one_at_a_time():
    """TQ 类（get_thsdk_*）必须先进先出、一次一个：峰值只能是 1。"""
    tq_tools = ["get_thsdk_quote", "get_thsdk_kline", "get_thsdk_chip", "get_thsdk_flow"]
    peak = Counter()

    async def ex(name, args):
        with peak:
            await asyncio.sleep(0.1)
        return f"{name}:ok"

    t0 = time.perf_counter()
    runs, _ = asyncio.run(run(Recipe(tq_tools), ["002050"], ex))
    elapsed = time.perf_counter() - t0

    assert peak.max == 1, f"TQ 类工具并发峰值 {peak.max}，必须为 1（单线程进程会被打崩）"
    assert elapsed >= 0.4, "TQ 类是串行的，4×0.1s 不可能更快"
    assert [t for t, _ in runs[0].outputs] == tq_tools, "TQ 串行也得保持配方顺序"
    assert runs[0].ok == 4


def test_tq_lane_does_not_break_global_bound():
    """混跑：TQ lane 不能让总并发超过上限（TQ 也只是占一个槽）。"""
    tools = ["tool_a", "get_thsdk_q1", "tool_b", "scan_auction_anomaly", "tool_c", "get_auction_data"]
    total = Counter()
    tq = Counter(match=pl._is_tq_tool)

    async def ex(name, args):
        with total:
            if pl._is_tq_tool(name):
                with tq:
                    await asyncio.sleep(0.05)
            else:
                await asyncio.sleep(0.2)
        return f"{name}:ok"

    runs, errs = asyncio.run(run(Recipe(tools), ["002050"], ex))
    assert errs == []
    assert total.max <= pl.PIPELINE_CONCURRENCY, f"总峰值 {total.max} 超限"
    assert tq.max == 1, f"TQ lane 峰值 {tq.max}，必须为 1"
    assert [t for t, _ in runs[0].outputs] == tools


def test_failures_are_isolated_and_counted(monkeypatch):
    """一个抛异常 / 一个超时 / 一个空文本：其余 3 个照跑，账要平。"""
    monkeypatch.setattr(pl, "PER_TOOL_TIMEOUT", 0.3)
    tools = ["ok_a", "boom", "slow", "blank", "ok_b", "ok_c"]

    async def ex(name, args):
        if name == "boom":
            raise RuntimeError("x")
        if name == "slow":
            await asyncio.sleep(5)
        if name == "blank":
            return ""
        await asyncio.sleep(0.1)
        return f"{name}:ok"

    runs, errs = asyncio.run(run(Recipe(tools), ["002050"], ex))
    pr = runs[0]
    assert pr.ok == 3 and pr.failed == 3, f"ok={pr.ok} failed={pr.failed}"
    assert [t for t, _ in pr.outputs] == ["ok_a", "ok_b", "ok_c"], "成功项仍按配方顺序"
    assert "boom:RuntimeError" in errs and "slow:timeout" in errs
    assert not any(e.startswith("blank:") for e in errs), "空文本算失败但不进 errors 明细（与串行版一致）"


def test_trace_records_every_tool_in_order():
    """轨迹必须每个工具一条，且顺序 == 配方顺序（治理页/ Jaccard 都靠它）。"""

    class Trace:
        def __init__(self):
            self.steps = []

        def step(self, tool, args=None, *, ok=True, error=None, result=None, elapsed_ms=None):
            self.steps.append((tool, ok, error, elapsed_ms))

    async def ex(name, args):
        if name == "b":
            raise RuntimeError("x")
        await asyncio.sleep(0.05)
        return "data"

    tr = Trace()
    tools = ["a", "b", "c", "d", "e"]
    runs, _ = asyncio.run(run(Recipe(tools), ["002050"], ex, trace=tr))

    assert len(tr.steps) == len(tools), f"trace 条数 {len(tr.steps)} != 工具数 {len(tools)}"
    assert [s[0] for s in tr.steps] == tools
    assert tr.steps[1][1] is False and tr.steps[0][1] is True
    assert all(isinstance(s[3], int) and s[3] >= 0 for s in tr.steps), "每条都要有耗时"


def test_symbols_stay_sequential():
    """标的之间仍串行：第二只票的工具不能和第一只的并发跑。"""
    live = {"n": 0, "max": 0}

    async def ex(name, args):
        live["n"] += 1
        live["max"] = max(live["max"], live["n"])
        await asyncio.sleep(0.1)
        live["n"] -= 1
        return f"{name}:{args['symbol']}"

    runs, _ = asyncio.run(run(Recipe(ORDER_TOOLS[:3]), ["002050", "301128"], ex))
    assert [r.symbol for r in runs] == ["002050", "301128"]
    assert all(r.ok == 3 for r in runs)
    assert live["max"] <= pl.PIPELINE_CONCURRENCY, "多标的不能把并发叠起来"


def test_concurrency_env_override(monkeypatch):
    """env 可覆盖并发度（运维出事时可一键退回串行）。"""
    monkeypatch.setenv("PIPELINE_CONCURRENCY", "1")
    assert pl._concurrency() == 1

    peak = Counter()

    async def ex(name, args):
        with peak:
            await asyncio.sleep(0.05)
        return "ok"

    asyncio.run(run(Recipe(ORDER_TOOLS[:3]), ["002050"], ex))
    assert peak.max == 1, "PIPELINE_CONCURRENCY=1 必须完全串行"

    monkeypatch.setenv("PIPELINE_CONCURRENCY", "not-a-number")
    assert pl._concurrency() == pl.PIPELINE_CONCURRENCY


def test_run_stream_emits_stage_per_tool_and_keeps_results():
    """流式版复用同一套并发语义：每个工具一条进度，最后给 runs。"""

    async def ex(name, args):
        await asyncio.sleep(0.05)
        return f"{name}:ok"

    events = []

    async def go():
        async for k, v in run_stream(Recipe(ORDER_TOOLS[:4]), ["002050"], ex):
            events.append((k, v))

    asyncio.run(go())
    stages = [v for k, v in events if k == "stage"]
    assert len(stages) == 4, f"进度条数 {len(stages)} != 工具数 4"
    assert all("正在取数" in s for s in stages)
    assert f"/4" in stages[0]
    kind, payload = events[-1]
    assert kind == "runs"
    runs, errs = payload
    assert errs == [] and runs[0].ok == 4
    assert [t for t, _ in runs[0].outputs] == ORDER_TOOLS[:4]
