"""提案采集工具的可见性契约（对应 specs/harness/recipes.md 的 RECIPE-006）。

背景：`note_new_dimension` 原本 `schema=None`，模型看不见 → 提案表恒空、面板永远没数据。
改成可见之后，风险换了个方向：**兜底路径同时服务闲聊/问价**，采集工具混进去
等于让模型在闲聊里自由写库（"让模型随手记"把表塞满垃圾维度）。

所以契约是两头都要钉住：
  ① 必须有 schema，且**只挂在一份分析配方**（`deep_diagnosis`）里 —— 否则是死管道；
  ② 必须 `fallback=False`，**不得进无配方兜底** —— 否则是脏数据。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

RECIPE_DIR = Path(__file__).resolve().parent.parent / "src" / "harness" / "recipes"
TOOL = "note_new_dimension"


def _recipe(rid: str) -> dict:
    d = yaml.safe_load((RECIPE_DIR / f"{rid}.yaml").read_text(encoding="utf-8")) or {}
    d.setdefault("id", rid)
    return d


def test_collection_tool_is_visible_to_model():
    """@spec RECIPE-006

    schema 必须在（否则模型看不见，采集链路等于没有入口）。
    """
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY, chat_tool_schemas

    tool = CHAT_TOOL_REGISTRY.get(TOOL)
    assert tool is not None, f"{TOOL} 未注册"
    assert tool.schema is not None, f"{TOOL} 仍是 handler-only，模型看不见 → 提案表恒空"

    exported = {str((s.get("function") or {}).get("name") or "") for s in chat_tool_schemas()}
    assert TOOL in exported, f"{TOOL} 没进 chat_tool_schemas() 导出面"


def test_collection_tool_schema_is_well_formed():
    """@spec RECIPE-006

    上一次工具 schema 写坏（description 尾逗号变 tuple → JSON 数组）直接把整条问答打成 500，
    所以逐个字段钉类型，不依赖「作者不手抖」。
    """
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    schema = CHAT_TOOL_REGISTRY[TOOL].schema or {}
    fn = schema.get("function") or {}

    assert schema.get("type") == "function"
    assert fn.get("name") == TOOL
    assert isinstance(fn.get("description"), str) and fn["description"].strip()

    params = fn.get("parameters") or {}
    assert params.get("type") == "object"
    props = params.get("properties") or {}
    assert set(props) == {"dimension", "reason", "symbol"}
    for key, spec in props.items():
        assert isinstance(spec, dict), f"{key} 的 spec 必须是 dict（尾逗号会变 tuple）"
        assert spec.get("type") == "string", f"{key} 的类型必须是 string"
        assert isinstance(spec.get("description"), str) and spec["description"].strip()
    assert sorted(params.get("required") or []) == ["dimension", "reason"]


def test_collection_tool_is_not_mounted_in_any_recipe():
    """@spec RECIPE-006 — 采集工具不挂任何配方（2026-10-22 改道后的契约）。

    挂着它 = 每轮一次**必然失败**的调用：`agnes-3.0-flash` 在连续 10 个只吃 `symbol`
    的工具之后，会对第 11 个工具无视 schema 直接照抄 `{"symbol": …}`（9 次实验 6 次；
    把 symbol 从 schema 里删掉也照吐）。采集改由 `src/core/gap_collector.py` 承担。
    """
    mounted = []
    for p in sorted(RECIPE_DIR.glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if TOOL in (d.get("tools") or []):
            mounted.append(p.stem)
    assert mounted == [], f"采集工具不该挂任何配方（模型调不对参数），实际挂在: {mounted}"


def test_deep_diagnosis_prompt_does_not_ask_for_tool_call():
    """@spec RECIPE-006 — 提示词里不许再要求模型调这个工具（那条路已被证伪）。"""
    frag = str(_recipe("deep_diagnosis").get("prompt_fragment") or "")
    assert TOOL not in frag, "deep_diagnosis 提示词不该再要求调用采集工具"


def test_deep_diagnosis_keeps_the_miss_marker():
    """@spec RECIPE-006 — 采集靠「未取到/暂无」触发，提示词必须保留这条要求。

    删掉这句 → 回答里不再有标记 → gap_collector 永不触发 → 提案表静默归零。
    """
    frag = str(_recipe("deep_diagnosis").get("prompt_fragment") or "")
    assert ("暂无" in frag) or ("未取到" in frag), (
        "deep_diagnosis 提示词必须要求「取不到就写暂无/未取到」—— 那是缺口采集的触发信号"
    )


def test_gap_collector_is_the_collection_path():
    """@spec RECIPE-006 — 采集入口必须存在（被删掉就静默归零）。"""
    from src.core import gap_collector

    for attr in ("should_check", "collect", "spawn", "parse_gaps"):
        assert hasattr(gap_collector, attr), f"gap_collector 缺 {attr}"
    assert "deep_diagnosis" in gap_collector.WATCH_RECIPES



def test_deep_diagnosis_budget_covers_mounted_tools():
    """@spec RECIPE-003 — 挂了但预算不够，排在后面的工具永远轮不到。"""
    d = _recipe("deep_diagnosis")
    tools = d.get("tools") or []
    budget = int((d.get("budget") or {}).get("max_tool_calls") or 0)
    assert budget >= len(tools), f"deep_diagnosis 预算 {budget} < 挂载工具数 {len(tools)}"


def test_collection_tool_declares_no_fallback():
    """@spec RECIPE-006 — 兜底路径服务闲聊/问价，采集工具绝不能进。"""
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY, no_fallback_tool_names

    assert CHAT_TOOL_REGISTRY[TOOL].fallback is False
    assert TOOL in no_fallback_tool_names()


def test_collection_tool_does_not_charge_tool_budget():
    """@spec RECIPE-006 — 记录类工具不吃取数预算。

    2026-10-22 生产实测：它是第 11 次调用（预算恰好 11），模型只传了 symbol、没传
    dimension/reason，被参数校验拦下；此时预算已尽，无法重试 → 提案没入库。
    只记录的工具不产出证据，不该和取数维度抢预算。
    """
    from src.agents.chat.registry import CHAT_TOOL_REGISTRY

    assert CHAT_TOOL_REGISTRY[TOOL].charges_budget is False
    # 取数工具仍然要计费（口径不能被这次改动带歪）
    assert CHAT_TOOL_REGISTRY["get_stock_quote"].charges_budget is True
    assert CHAT_TOOL_REGISTRY["get_trend_profile"].charges_budget is True


def test_budget_helper_lets_collector_through_when_exhausted():
    """@spec RECIPE-006 — 预算是满的时候，记录类工具仍然放行（且不吃计数）。"""
    from src.agents.chat.registry import charge_tool_budget

    budget = {"used": 11, "max": 11}
    assert charge_tool_budget(budget, TOOL, "深度诊断") is None
    assert budget["used"] == 11, "记录类工具不该动 budget.used"

    # 同一份已满预算下，取数工具必须被拒（口径没被放宽）
    denied = charge_tool_budget(budget, "get_stock_quote", "深度诊断")
    assert denied and "预算" in denied

    # 未满时取数工具正常计费
    budget2 = {"used": 3, "max": 11}
    assert charge_tool_budget(budget2, "get_stock_quote", "深度诊断") is None
    assert budget2["used"] == 4


@pytest.mark.parametrize("text", ["你好", "今天天气不错", "谢谢"])
def test_casual_chat_fallback_excludes_collection_tool(text):
    """@spec RECIPE-006 — 闲聊走 default 兜底，采集工具必须不在其中。"""
    from src.agents.chat.registry import chat_tool_schemas, schema_tool_name
    from src.harness.routing import route_for_request

    route = route_for_request(text, chat_tool_schemas())
    names = {schema_tool_name(s) for s in route.tools}
    assert TOOL not in names, f"闲聊「{text}」的工具集里出现了 {TOOL}"


def test_stock_question_fallback_excludes_collection_tool(monkeypatch):
    """@spec RECIPE-006 — 未命配方的个股问题也走兜底，同样不许带采集工具。"""
    import src.core.symbol_guard as guard
    from src.agents.chat.registry import chat_tool_schemas, schema_tool_name
    from src.harness.routing import route_for_request

    monkeypatch.setattr(guard, "is_stock_question", lambda _t: True)
    route = route_for_request("随便看看这个", chat_tool_schemas())
    assert route.reason == "stock_question"
    names = {schema_tool_name(s) for s in route.tools}
    assert TOOL not in names


def test_fallback_is_not_emptied_by_the_filter():
    """@spec RECIPE-006 — 过滤只该摘掉采集类工具，不许顺手清空整个工具集。"""
    from src.agents.chat.registry import chat_tool_schemas, no_fallback_tool_names, schema_tool_name
    from src.harness.routing import route_for_request

    schemas = chat_tool_schemas()
    route = route_for_request("你好", schemas)
    assert len(route.tools) == len(schemas) - len(no_fallback_tool_names())
    assert len(route.tools) > 50, "兜底工具集被清空了，这会让闲聊/问价全部失能"
    # 普通取数工具必须还在
    names = {schema_tool_name(s) for s in route.tools}
    assert "get_stock_quote" in names
