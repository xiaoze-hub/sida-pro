"""全市场确定性管线（src/harness/pipeline_market.py）的守卫测试（2026-10-23）。

守的是**取数纪律**，不是措辞：
  1. 市场级工具（schema.required 为空）必须用 `{}` 调 —— 多塞一个 symbol 就是假参数；
  2. 标的级工具没自选时必须**不跑**但留轨迹（no_symbol_scope），有自选时按上限展开；
  3. handler-only / 参数填不上的工具一律 fail-closed（不盲调，也不悄悄复活隐藏工具）；
  4. 全部取数失败 → 返回 None，调用方回落多轮 loop（管线挂了不许变成没答案）；
  5. 证据裁剪（单工具 2500 / 合计 12000）；
  6. 流式作答先报进度、最后 done；全失败时**不产出 done**（回落的信号）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.harness.pipeline import PER_TOOL_CHARS, TOTAL_EVIDENCE_CHARS
from src.harness.pipeline_market import (
    MAX_SYMBOL_TOOLS,
    MAX_WATCHLIST_SYMBOLS,
    MarketRun,
    answer_market,
    answer_market_stream,
    build_steps,
    compose_market_prompt,
    run_market_pipeline,
)
from src.harness.recipes import Recipe, _parse, list_recipes, load_recipes, match_recipe


# ------------------------------------------------------------------ 测试替身


class _Trace:
    """轨迹句柄的最小实现：只要 step(...)，字段与 CopilotTrace.step 对齐。"""

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def step(self, tool, args=None, *, ok=True, error=None, result=None, elapsed_ms=None):
        self.steps.append({"tool": str(tool), "args": dict(args or {}), "ok": bool(ok),
                           "error": error, "result": result})

    def errors_of(self, tool: str) -> list[str]:
        return [s["error"] for s in self.steps if s["tool"] == tool and not s["ok"]]


class _Exec:
    """记录每次调用的 (tool, args)；`out` 决定返回值，异常值直接抛。"""

    def __init__(self, out: dict | None = None, default: str = "证据正文") -> None:
        self.calls: list[tuple[str, dict]] = []
        self.out = out or {}
        self.default = default

    async def __call__(self, name: str, args: dict) -> str:
        self.calls.append((name, dict(args)))
        val = self.out.get(name, self.default)
        if isinstance(val, Exception):
            raise val
        return val

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]

    def args_of(self, name: str) -> list[dict]:
        return [a for n, a in self.calls if n == name]


def _recipe(tools, sections=("结论", "口径")) -> Recipe:
    return Recipe(
        id="t_market", label="测试全市场", tools=tuple(tools),
        output_sections=tuple(sections),
        prompt_fragment="你是测试智能体。\n  【格式】严格使用这些段落名：结论 / 口径",
        pipeline=True, scope="market",
    )


# ------------------------------------------------- 1. 市场级工具必须用 {} 调


async def test_market_tools_run_with_empty_args():
    recipe = _recipe(["get_hot_stocks", "get_sentiment_cycle", "get_market_news"])
    ex = _Exec()
    trace = _Trace()
    run = await run_market_pipeline(recipe, ex, market="CN", trace=trace)

    assert run is not None and run.usable
    assert run.ok == 3 and run.failed == 0
    # 关键断言：市场级工具的参数**必须是空 dict**（多给 symbol 就是假参数）
    for name, args in ex.calls:
        assert args == {}, f"{name} 收到了非空参数 {args}"
    assert [s["ok"] for s in trace.steps] == [True, True, True]


async def test_handled_tools_keep_recipe_order():
    """证据块顺序 == 配方顺序（审计要能一眼对上第 N 个工具 → 第 N 段）。"""
    recipe = _recipe(["get_sentiment_cycle", "get_hot_stocks", "get_market_news"])
    run = await run_market_pipeline(recipe, _Exec(), market="CN")
    assert [label for label, _ in run.outputs] == [
        "get_sentiment_cycle", "get_hot_stocks", "get_market_news",
    ]


# --------------------------------------- 2. 标的级工具：没自选跳过、有自选展开


async def test_symbol_tool_skipped_without_watchlist_but_traced():
    recipe = _recipe(["get_hot_stocks", "get_stock_quote"])
    ex = _Exec()
    trace = _Trace()
    run = await run_market_pipeline(recipe, ex, market="CN", trace=trace)

    assert ex.names() == ["get_hot_stocks"], "没有自选时不许拿空 symbol 去调标的级工具"
    assert run.ok == 1
    assert "get_stock_quote" in run.skipped
    assert trace.errors_of("get_stock_quote") == ["no_symbol_scope"]
    # 结论提示词必须点明"这一维度没取到"，否则模型会把"没查"写成"没有"
    _system, user_block = compose_market_prompt(recipe, run, "今天怎么看")
    assert "get_stock_quote" in user_block
    assert "未跑 1" in user_block


async def test_symbol_tools_expanded_and_caps_respected():
    # 4 个标的级工具 + 10 个自选：每只最多 3 个工具、最多 8 只
    tools = ["get_stock_quote", "get_main_intent", "get_decision_pioneer",
             "get_technical_analysis"]
    watchlist = [f"60000{i}" for i in range(10)]
    watchlist[3] = ""      # 空串必须被过滤，不许变成一次 symbol="" 的调用
    recipe = _recipe(tools)
    ex = _Exec()
    trace = _Trace()
    run = await run_market_pipeline(recipe, ex, market="CN", trace=trace,
                                    watchlist=watchlist + ["600000"])  # 重复项也要去重

    syms = [a["symbol"] for a in ex.args_of("get_stock_quote")]
    assert len(syms) == min(MAX_WATCHLIST_SYMBOLS, len(set(filter(None, watchlist))))
    assert "" not in syms
    assert len(syms) == len(set(syms)), "同一只票不许重复调用"
    assert ex.names().count("get_stock_quote") <= MAX_WATCHLIST_SYMBOLS
    # 第 4 个标的级工具超出"每只最多 3 个"上限 → 不跑、留轨迹
    assert "get_technical_analysis" not in ex.names()
    assert trace.errors_of("get_technical_analysis") == ["symbol_tool_cap"]
    assert run.ok == 3 * MAX_WATCHLIST_SYMBOLS
    assert MAX_SYMBOL_TOOLS == 3


async def test_symbol_args_carry_market_only_when_declared():
    """`market` 只在 schema 声明了才给（get_stock_sectors 只声明 symbol）。"""
    recipe = _recipe(["get_stock_quote", "get_stock_sectors"])
    ex = _Exec()
    await run_market_pipeline(recipe, ex, market="CN", watchlist=["600111"])

    assert ex.args_of("get_stock_quote") == [{"symbol": "600111", "market": "CN"}]
    assert ex.args_of("get_stock_sectors") == [{"symbol": "600111"}]


# -------------------------------------------- 3. 参数填不上 / 隐藏工具 → 不跑


async def test_unsatisfiable_and_hidden_tools_are_not_called():
    recipe = _recipe(["search_symbols", "get_opportunities", "get_hot_stocks"])
    ex = _Exec()
    trace = _Trace()
    run = await run_market_pipeline(recipe, ex, market="CN", trace=trace)

    assert ex.names() == ["get_hot_stocks"], "参数填不上或 handler-only 的工具绝不许盲调"
    assert trace.errors_of("search_symbols") == ["unsatisfiable_args:keyword"]
    # get_opportunities 是 schema=None 的 handler-only 工具（荐股口径，故意不给模型看）
    assert trace.errors_of("get_opportunities") == ["no_schema"]
    assert set(run.skipped) == {"search_symbols", "get_opportunities"}


async def test_unknown_tool_is_skipped_fail_closed():
    recipe = _recipe(["get_hot_stocks", "totally_made_up_tool"])
    ex = _Exec()
    trace = _Trace()
    await run_market_pipeline(recipe, ex, market="CN", trace=trace)

    assert ex.names() == ["get_hot_stocks"]
    assert trace.errors_of("totally_made_up_tool") == ["unknown_tool"]


# --------------------------------------------------- 4. 全失败 = 回落信号 None


async def test_all_fail_returns_none_for_fallback():
    recipe = _recipe(["get_hot_stocks", "get_sentiment_cycle"])
    ex = _Exec(out={"get_hot_stocks": RuntimeError("boom"), "get_sentiment_cycle": None})
    trace = _Trace()
    run = await run_market_pipeline(recipe, ex, market="CN", trace=trace)

    assert run is None, "一个维度都没取到时必须返回 None，让调用方回落多轮 loop"
    assert trace.errors_of("get_hot_stocks") == ["RuntimeError"]
    assert trace.errors_of("get_sentiment_cycle") == ["empty"]


async def test_all_skipped_returns_none():
    """只有标的级工具 + 没有自选 = 一次调用都没有 → 同样是回落信号。"""
    run = await run_market_pipeline(_recipe(["get_stock_quote"]), _Exec(), market="CN")
    assert run is None


async def test_wall_clock_budget_stops_queueing(monkeypatch):
    """墙钟预算到点：不再发请求（否则最坏要用户等 8 分钟）。"""
    monkeypatch.setenv("PIPELINE_MARKET_BUDGET", "0")
    ex = _Exec()
    trace = _Trace()
    run = await run_market_pipeline(_recipe(["get_hot_stocks"]), ex, market="CN", trace=trace)

    assert run is None
    assert ex.calls == []
    assert trace.errors_of("get_hot_stocks") == ["budget_exhausted"]


# ------------------------------------------------------------- 5. 证据裁剪


async def test_evidence_clipping_per_tool_and_total():
    huge = "甲" * (PER_TOOL_CHARS * 4)
    tools = ["get_market_news", "get_hot_stocks", "get_sentiment_cycle",
             "get_strategy_signals", "get_market_anomalies", "get_notifications"]
    recipe = _recipe(tools)
    ex = _Exec(out={n: huge for n in tools})
    run = await run_market_pipeline(recipe, ex, market="CN")

    _system, user_block = compose_market_prompt(recipe, run, "复盘")
    assert "…（后略）" in user_block, "单个工具超长必须截断"
    # 单工具上限：任一段正文都不超过 PER_TOOL_CHARS
    per_block = [b.count("甲") for b in user_block.split("--- ")[1:]]
    assert per_block and max(per_block) == PER_TOOL_CHARS
    # 合计上限：后面的工具被整段省略，只留一句提示
    assert "证据总量已达上限" in user_block
    assert user_block.count("甲") <= TOTAL_EVIDENCE_CHARS
    assert len(user_block) <= TOTAL_EVIDENCE_CHARS + 2000


# ------------------------------------------------------- 6. 流式：stage → done


class _StreamAI:
    """最小 ai_client：流式产出 delta + 最终消息；记录收到的 messages。"""

    def __init__(self, text: str = "结论正文：情绪冰点。") -> None:
        self.text = text
        self.calls: list[list[dict]] = []

    async def chat_with_tools_stream(self, msgs, tools=None, temperature=None):
        assert tools == [], "全市场管线结论轮不给工具（取数已由 harness 完成）"
        self.calls.append(list(msgs))
        yield "delta", self.text
        yield "assistant", SimpleNamespace(content=self.text)


async def test_stream_yields_stages_then_done():
    recipe = _recipe(["get_hot_stocks", "get_sentiment_cycle"])
    ex = _Exec()
    ai = _StreamAI()

    events = [ev async for ev in answer_market_stream(ai, recipe, ex, "今天怎么看")]

    kinds = [k for k, _ in events]
    assert kinds[0] == "stage" and kinds[-1] == "done"
    stages = [p for k, p in events if k == "stage"]
    assert len(stages) == 2, f"每个执行的工具一条进度，实际 {stages}"
    assert all("正在取数" in s for s in stages)
    done = events[-1][1]
    assert "结论正文" in done
    # 结论轮的证据块必须进到 user message 里（不然模型只能瞎写）
    assert "get_hot_stocks" in ai.calls[0][-1]["content"]


async def test_stream_falls_back_when_everything_failed():
    recipe = _recipe(["get_hot_stocks", "get_sentiment_cycle"])
    ex = _Exec(out={"get_hot_stocks": RuntimeError("down"), "get_sentiment_cycle": ""})
    ai = _StreamAI()

    events = [ev async for ev in answer_market_stream(ai, recipe, ex, "今天怎么看")]

    assert all(k != "done" for k, _ in events), "全部失败时不许产出 done（那是回落的信号）"
    assert ai.calls == [], "没证据就不该让模型写结论"


class _SyncAI(_StreamAI):
    """非流式 ai_client：chat_with_tools(msgs, tools=[]) → 一条消息。"""

    async def chat_with_tools(self, msgs, tools=None, temperature=None):
        assert tools == [], "全市场管线结论轮不给工具"
        self.calls.append(list(msgs))
        return SimpleNamespace(content=self.text)


async def test_answer_market_non_stream_and_fallback():
    recipe = _recipe(["get_hot_stocks"])
    ok_ai = _SyncAI("结论：情绪冰点。")
    text = await answer_market(ok_ai, recipe, _Exec(), "今天怎么看")
    assert text and "情绪冰点" in text
    assert "get_hot_stocks" in ok_ai.calls[0][-1]["content"]

    # 全部取数失败 → None（调用方据此回落多轮 loop）
    bad_ai = _SyncAI()
    assert await answer_market(bad_ai, recipe,
                               _Exec(out={"get_hot_stocks": RuntimeError("x")}),
                               "今天怎么看") is None
    assert bad_ai.calls == []


# ------------------------------------------------ 7. Recipe.scope 解析与路由


def test_recipes_still_parse_and_scope_defaults_to_symbol():
    recipes = load_recipes()
    assert len(recipes) == 11
    assert {r.scope for r in recipes.values()} <= {"symbol", "market"}
    # 5 份单标的管线配方不写 scope → 默认 symbol（旧行为，一个字都不许变）
    for rid in ("chip_analyst", "deep_diagnosis", "demon_diagnosis",
                "intraday_analyst", "left_diagnosis"):
        assert recipes[rid].scope == "symbol", rid
        assert recipes[rid].pipeline is True, rid
    # 2026-10-23 新接的全市场配方
    for rid in ("auction_hunter", "premarket_strategist",
                "postmarket_reviewer", "intraday_watcher"):
        assert recipes[rid].scope == "market", rid
        assert recipes[rid].pipeline is True, rid
    # 风控裁判要的是"每只持仓读数"，取数阶段拿不到持仓代码 → 明确不接（不许假装接上）
    assert recipes["risk_judge"].pipeline is False
    assert recipes["risk_judge"].scope == "symbol"


def test_scope_parsed_from_yaml(tmp_path):
    p = tmp_path / "x.yaml"
    p.write_text(
        "id: x\nlabel: X\ntools: [get_hot_stocks]\noutput_sections: [结论]\n"
        "prompt_fragment: 结论\nbudget: {max_tool_calls: 3}\nscope: MARKET\n",
        encoding="utf-8",
    )
    assert _parse(p).scope == "market"          # 大小写不敏感
    p.write_text(
        "id: y\nlabel: Y\ntools: [get_hot_stocks]\noutput_sections: [结论]\n"
        "prompt_fragment: 结论\n",
        encoding="utf-8",
    )
    assert _parse(p).scope == "symbol"          # 未声明 → 默认 symbol
    p.write_text(
        "id: z\nlabel: Z\ntools: [get_hot_stocks]\noutput_sections: [结论]\n"
        "prompt_fragment: 结论\nscope: nonsense\n",
        encoding="utf-8",
    )
    assert _parse(p).scope == "symbol"          # 写错 → 退回旧行为，不猜


def test_routing_unchanged_for_market_recipes():
    assert match_recipe("今天竞价谁爆量").id == "auction_hunter"
    assert match_recipe("帮我复盘今天").id == "postmarket_reviewer"
    assert match_recipe("现在盘中持仓要不要减").id in {"intraday_watcher", "intraday_analyst"}
    assert match_recipe("用一句话说明你能做什么") is None
    assert {r.id for r in list_recipes()} == {
        "premarket_strategist", "auction_hunter", "intraday_watcher",
        "postmarket_reviewer", "risk_judge", "chip_analyst", "intraday_analyst",
        "deep_diagnosis", "demon_diagnosis", "left_diagnosis", "theme_leader",
    }


def test_market_recipe_plans_are_symbol_safe():
    """真实配方的取数计划：市场级工具全在，标的级工具按上限展开，绝不空 symbol。"""
    recipes = load_recipes()
    steps = build_steps(recipes["postmarket_reviewer"], ["600111", "002050"], "CN")
    # 2026-10-23: 复盘必须从三池同源证据开始（涨停/炸板/跌停/晋级率），见 specs REPLAY-001。
    # 原先首步是 get_sentiment_cycle —— 那条路径里根本没有涨停池，导致复盘写「0家涨停」。
    first = [s.label for s in steps if not s.skip][0]
    assert first == "get_market_replay"
    assert all(s.args.get("symbol") != "" for s in steps if s.symbol)
    no_wl = build_steps(recipes["auction_hunter"], None, "CN")
    assert {s.tool for s in no_wl if not s.skip} == {
        "scan_auction_anomaly", "get_auction_data", "get_hot_stocks",
    }
