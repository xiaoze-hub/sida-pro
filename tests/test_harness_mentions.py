"""D1 验收：内置智能体配方 + @提及解析 + 工具白名单/预算。

用户视角要成立的两件事：
  1. `@股票` → 本轮锁定标的（不再把 600111 分析成 002361）
  2. `@智能体` → 本轮换成一套配方（工具白名单 + 提示词片段 + 预算）
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from src.harness.recipes import get_recipe, list_recipes, load_recipes
from src.web.api import chat as chat_api
from src.harness.context import (
    Mention,
    apply_mentions as _apply_mentions,
    inject_recipe as _inject_recipe,
)
from src.web.api.chat import SendMessageBody


class _DB:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def _conv(symbol=None, market=None):
    return SimpleNamespace(stock_symbol=symbol, stock_market=market)


def test_six_recipes_loaded_and_valid():
    """配方数量是显式契约：新增/删除配方必须同步这里。
    2026-10-21: 增 intraday_analyst(8) → 按标的画像增 demon/left/theme_leader(11)。"""
    recipes = list_recipes()
    assert len(recipes) == 11
    assert {r.id for r in recipes} == {
        "premarket_strategist", "auction_hunter", "intraday_watcher",
        "postmarket_reviewer", "risk_judge", "chip_analyst", "intraday_analyst",
        "deep_diagnosis", "demon_diagnosis", "left_diagnosis", "theme_leader",
    }
    for r in recipes:
        assert r.label and r.prompt_fragment
        assert r.output_sections, r.id
        assert 1 <= r.max_tool_calls <= 30, r.id


def test_recipe_tools_all_exist_in_registry():
    """配方里写错工具名不会报错，只会在运行时静默变成"模型没法用这个工具" —— 必须静态拦住。"""
    known = set(chat_api.CHAT_TOOL_REGISTRY)
    unknown = {r.id: [t for t in r.tools if t not in known] for r in list_recipes()}
    bad = {k: v for k, v in unknown.items() if v}
    assert not bad, f"配方引用了不存在的工具: {bad}"


def test_public_meta_hides_prompt():
    meta = get_recipe("risk_judge").public()
    assert "prompt_fragment" not in meta
    assert meta["tool_count"] >= 1 and meta["when"]


def test_mention_stock_binds_and_overrides_binding():
    conv, db = _conv("600111", "SH"), _DB()
    body = SendMessageBody(content="看看这个", mentions=[Mention(type="stock", value="002361")])
    _apply_mentions(db, conv, body)
    assert conv.stock_symbol == "002361"
    assert conv.stock_market == "SZ"
    assert db.commits == 1


def test_mention_agent_selects_recipe_and_injects_prompt():
    conv, db = _conv(), _DB()
    body = SendMessageBody(content="复盘", mentions=[Mention(type="agent", value="postmarket_reviewer")])
    recipe, note = _apply_mentions(db, conv, body)
    assert recipe and recipe.id == "postmarket_reviewer"
    assert note is None

    messages = [{"role": "system", "content": "BASE"}]
    _inject_recipe(messages, recipe, note)
    assert "盘后复盘官" in messages[0]["content"]
    assert "只做当日收盘后的事实复盘" in messages[0]["content"]
    assert "本轮只允许调用这些工具" in messages[0]["content"]


def test_unknown_agent_is_ignored_not_fatal():
    conv, db = _conv(), _DB()
    body = SendMessageBody(content="x", mentions=[Mention(type="agent", value="nope")])
    recipe, _ = _apply_mentions(db, conv, body)
    assert recipe is None


def test_multi_stock_falls_back_to_first_with_note():
    conv, db = _conv(), _DB()
    body = SendMessageBody(content="对比", mentions=[
        Mention(type="stock", value="600111"), Mention(type="stock", value="002361"),
    ])
    recipe, note = _apply_mentions(db, conv, body)
    assert conv.stock_symbol == "600111"
    assert note and "最多支持 1 只" in note


def _run_tool(name: str, args: dict, setup):
    async def run():
        tokens = setup()
        try:
            return await chat_api._execute_tool(None, name, args)
        finally:
            chat_api._exit_request_ctx(tokens)

    return asyncio.run(run())


def test_tool_outside_whitelist_is_rejected():
    def setup():
        return [
            (chat_api._REQ_TOOLS, chat_api._REQ_TOOLS.set(frozenset({"get_watchlist"}))),
            (chat_api._REQ_RECIPE_LABEL, chat_api._REQ_RECIPE_LABEL.set("盘后复盘官")),
        ]

    out = _run_tool("get_portfolio", {}, setup)
    assert "盘后复盘官" in out and "get_portfolio" in out


def test_budget_exhausts_after_max_calls():
    budget = {"max": 1, "used": 0}

    def setup():
        return [
            (chat_api._REQ_TOOLS, chat_api._REQ_TOOLS.set(None)),
            (chat_api._REQ_BUDGET, chat_api._REQ_BUDGET.set(budget)),
            (chat_api._REQ_RECIPE_LABEL, chat_api._REQ_RECIPE_LABEL.set("风控裁判")),
        ]

    first = _run_tool("no_such_tool", {}, setup)
    second = _run_tool("no_such_tool", {}, setup)
    assert "未知工具" in first          # 第一次：预算放行，落到工具不存在
    assert "预算（1 次）已用尽" in second


def test_recipe_tags_are_strings_even_when_yaml_coerces():
    """YAML 1.1 会把 `9:25` 当六十进制整数（565），join 时直接炸 —— 线上 /mentions 500 的根因。"""
    auction = get_recipe("auction_hunter")
    assert all(isinstance(w, str) for w in auction.when)
    assert "9:25" in auction.when                      # 保留原样，不是 565
    assert " ".join(auction.when) == " ".join(list(auction.when))


def test_recipe_prompt_pins_its_own_sections():
    """结构漂移（D3 实测盘中用 markdown 编号）靠配方自身把段落名钉死。"""
    for r in list_recipes():
        assert "严格使用这些段落名做标题" in r.prompt_fragment, r.id
        for sec in r.output_sections:
            assert sec in r.prompt_fragment, f"{r.id} 缺少段落 {sec}"
