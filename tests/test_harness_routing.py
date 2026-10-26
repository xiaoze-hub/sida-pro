"""确定性路由（2026-10-21）：同一问题必须落到同一份配方、同一套工具、同一温度。

由来：8 问题 × 3 次的方差实验里，同一问题工具集合 Jaccard 只有 0.52，
「全景深度诊断002050」三次分别调 4 / 0 / 3 个工具。让模型自由选工具就是方差源。
"""
import pytest

from src.harness.recipes import filter_schemas, match_recipe
from src.harness.routing import route_for_request

CASES = [
    ("全景数智深度诊断002050.SZ", "deep_diagnosis"),
    ("全景数智深度诊断 三花智控", "deep_diagnosis"),
    ("002050 这半小时撤单有没有变化？哪一分钟资金最猛", "intraday_analyst"),
    ("002050 分时和盘口现在什么状态", "intraday_analyst"),
    ("002050 筹码怎么样", "chip_analyst"),
    ("今天竞价谁爆量", "auction_hunter"),
    ("帮我复盘今天", "postmarket_reviewer"),
    ("持仓要不要止损", "risk_judge"),
    # 2026-10-24 创新 6：空头司令挑刺（红蓝对抗压力测试）必须确定性命中自己的配方
    ("002050 空头司令挑刺（压力测试）", "devil_advocate"),
    ("帮我做红蓝对抗压力测试", "devil_advocate"),
    ("用一句话说明你能做什么", None),
]


@pytest.mark.parametrize("q,expected", CASES)
def test_route_is_deterministic(q, expected):
    r = match_recipe(q)
    assert (r.id if r else None) == expected, f"{q} → {r.id if r else None}"


def test_route_stable_across_repeats():
    q = "全景数智深度诊断002050.SZ"
    assert len({match_recipe(q).id for _ in range(5)}) == 1


def test_route_narrows_tools_and_lowers_temperature():
    schemas = [
        {"type": "function", "function": {"name": n}}
        for n in ("get_stock_dossier", "get_chip_accumulation", "get_news", "get_orderbook")
    ]
    rt = route_for_request("全景数智深度诊断002050.SZ", schemas)
    assert rt.recipe is not None and rt.reason.startswith("recipe:")
    assert rt.temperature == 0.2
    names = {s["function"]["name"] for s in rt.tools}
    assert names == {"get_stock_dossier", "get_chip_accumulation"}, "白名单必须真的收窄"


def test_route_keeps_chitchat_wide():
    rt = route_for_request("用一句话说明你能做什么", [{"type": "function", "function": {"name": "x"}}])
    assert rt.recipe is None and rt.temperature == 0.5 and rt.reason == "default"


def test_forced_whitelist_wins():
    schemas = [{"type": "function", "function": {"name": n}} for n in ("a", "b")]
    rt = route_for_request("002050 筹码", schemas, forced=frozenset({"a"}))
    assert rt.reason == "mention" and {s["function"]["name"] for s in rt.tools} == {"a"}


def test_filter_schemas_fails_closed_on_whitelist_miss():
    """白名单一个都没命中时必须收紧到 0 工具, 不能回退全量。

    2026-10-22 修正: 旧实现是 `return out or schemas`, 白名单写错(工具名不存在)
    时反而把**全量工具**交给模型 —— 与白名单机制的目的正好相反。
    旧测试名 `test_filter_schemas_never_returns_empty` 断言的也是这个反直觉行为,
    名字里的意图("白名单写错不该让模型裸奔")与断言内容自相矛盾, 一并改正。

    @spec RECIPE-010
    """
    schemas = [{"type": "function", "function": {"name": "a"}}]
    assert filter_schemas(schemas, frozenset({"nonexistent"})) == [], (
        "白名单一个都没命中时必须返回 0 工具(fail-closed), "
        "不能回退到全量让模型裸奔"
    )


def test_persona_recipe_locks_recipe_and_narrows_tools():
    """画像命中必须直接锁配方：妖股不能被「筹码怎么样」这类关键词带偏到 chip_analyst。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    schemas = [{"type": "function", "function": {"name": n}} for n in CHAT_TOOL_REGISTRY]
    rt = route_for_request("002050 筹码怎么样", schemas, persona_recipe="demon_diagnosis")
    assert rt.recipe is not None and rt.recipe.id == "demon_diagnosis"
    assert rt.reason == "persona:demon_diagnosis"
    assert rt.temperature == 0.2
    names = {s["function"]["name"] for s in rt.tools}
    assert "get_demon_factor" in names and "get_chip_accumulation" not in names, "画像配方必须真收窄工具"


def test_persona_recipe_mention_still_wins():
    """@提及 > 画像：用户显式指定工具集时，画像不许覆盖。"""
    schemas = [{"type": "function", "function": {"name": n}} for n in ("a", "b")]
    rt = route_for_request("002050 筹码", schemas, forced=frozenset({"a"}), persona_recipe="demon_diagnosis")
    assert rt.reason == "mention" and {s["function"]["name"] for s in rt.tools} == {"a"}


def test_persona_recipe_unknown_id_falls_back():
    """配方 id 写错不许崩，也不许锁空工具集 —— 回落到常规路由。"""
    schemas = [{"type": "function", "function": {"name": "a"}}]
    rt = route_for_request("用一句话说明你能做什么", schemas, persona_recipe="no_such_recipe")
    assert rt.recipe is None and rt.reason == "default"
