"""判断类问题的路由兜底 + 名字解析（2026-10-22 生产事故回归）。

事故：用户问「北方稀土今日冲高回落…还能持有吗，是黎明前的借势洗盘，还是真出货」，
参谋答「这次没有取到任何行情数据（工具一次都没调用）」。

三层原因：
1. 正文只有名字没有 6 位代码 → `enter_request_ctx` 的 bound 为空；
2. 关键词表里没有「持有/洗盘/出货」→ 没命中任何配方；
3. 于是给模型全量 64 个工具让它自己选 → 它一个都没调 → 零工具拒答。
"""

from __future__ import annotations

import pytest

from src.core import symbol_guard
from src.harness.context import _is_judgment_question, route_with_persona


@pytest.fixture()
def fake_tq(monkeypatch):
    """把 TQ 名称检索换成固定表，顺带记录调用次数（验证缓存）。"""
    calls: list[str] = []

    def _fake(word: str):
        calls.append(word)
        if "北方稀土" in word or word in ("北方", "稀土"):
            return [("600111.SH", "北方稀土")]
        return []

    monkeypatch.setattr(symbol_guard, "_tq_match", _fake)
    monkeypatch.setattr(symbol_guard, "_TQ_MATCH_CACHE", {})
    return calls


def test_name_resolves_to_code(fake_tq):
    """只写名字时也要能解析出代码，否则整条分析链都空转。"""
    assert symbol_guard.first_code_from_names("北方稀土今日冲高回落，还能持有吗") == "600111"


def test_explicit_code_wins_and_is_not_rewritten(fake_tq):
    """正文已有 6 位代码时不许自作聪明改标的（resolve_mentions 的既有契约）。"""
    assert symbol_guard.first_code_from_names("600111 今天怎么样") is None
    assert fake_tq == []


def test_name_lookup_is_cached(fake_tq):
    """路由阶段每轮都会解析名字，不能每句都打一次单线程 17709。

    注意：一次解析可能对**多个候选词**各查一次（"北方稀土" 与 "北方"），
    所以断言的是「第二次解析不再新增调用」，而不是总数等于 1。
    """
    symbol_guard.first_code_from_names("北方稀土还能拿吗")
    first = len(fake_tq)
    assert first >= 1
    symbol_guard.first_code_from_names("北方稀土还能拿吗")
    assert len(fake_tq) == first, f"第二次应全部命中缓存, 又多打了 {len(fake_tq) - first} 次"


def test_unknown_name_returns_none(fake_tq):
    assert symbol_guard.first_code_from_names("完全不存在的东西怎么样") is None


@pytest.mark.parametrize(
    "text",
    [
        "北方稀土今日冲高回落跌一个多点，大盘环境也不好，还能持有吗，是黎明前的借势洗盘，还是真出货",
        "002361 是洗盘还是出货",
        "这票该不该卖",
    ],
)
def test_judgment_question_routes_to_deep_diagnosis(text):
    """判断类问题必须走确定性配方，而不是「全量工具让模型自选」。"""
    from src.agents.chat.registry import chat_tool_schemas

    schemas = chat_tool_schemas()
    route, _ = route_with_persona(None, text, "600111", True, schemas)
    assert route.recipe is not None, "没命中配方 → 模型会自己选工具(实测 0 次调用)"
    assert route.recipe.id == "deep_diagnosis"
    assert 0 < len(route.tools) < len(schemas), "配方必须真的收窄工具集"


@pytest.mark.parametrize("text", ["600111 多少钱", "600111 现价多少", "北方稀土现价"])
def test_price_only_question_is_not_forced_into_pipeline(text):
    """单纯问价不许触发 10 工具管线（这条是 2026-10 的既有教训）。"""
    from src.agents.chat.registry import chat_tool_schemas

    schemas = chat_tool_schemas()
    route, _ = route_with_persona(None, text, "600111", True, schemas)
    assert route.recipe is None
    assert len(route.tools) == len(schemas)


def test_judgment_words():
    assert _is_judgment_question("还能持有吗")
    assert _is_judgment_question("是洗盘还是出货")
    assert not _is_judgment_question("多少钱")
    assert not _is_judgment_question("今天的成交量")
