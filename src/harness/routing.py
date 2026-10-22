"""请求级路由（2026-10-21）：把"用哪些工具、什么温度"从模型手里拿回来。

实测依据（8 问题 × 3 次，数据冻结的午休时段）：
- 同一问题工具集合 Jaccard 0.52；最差「全景深度诊断」三次 4 / 0 / 3 个工具；
- 耗时 CV 0.55（同一问题 20s ~ 117s）。
方差主要来自"模型自选工具 + 0.5 温度"。这个模块只做两件确定性的事：
1. 命中配方关键词 → 工具 schema 收紧到配方白名单（运行时还有 REQ_TOOLS 兜底拒绝）；
2. 数据类问题温度降到 0.2（抄数字不需要创造力）。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Route:
    recipe: object | None = None
    tools: list[dict] = field(default_factory=list)
    temperature: float = 0.5
    reason: str = "default"


def route_for_request(text: str, all_schemas: list[dict],
                      forced: frozenset[str] | set[str] | None = None,
                      persona_recipe: str | None = None) -> Route:
    """决定本轮工具集与温度。

    优先级：@提及白名单 > 标的画像（persona_recipe）> 关键词路由 > 默认。
    画像是确定性判定（stock_persona.detect），比关键词更可靠 ——
    妖股用「趋势+估值」的配方分析必然输出废话，所以画像命中时直接锁定配方。
    """
    from src.harness.recipes import filter_schemas, get_recipe, match_recipe
    from src.core.symbol_guard import is_stock_question

    if forced:
        return Route(recipe=None, tools=filter_schemas(all_schemas, forced),
                     temperature=0.2 if is_stock_question(text) else 0.3,
                     reason="mention")

    if persona_recipe:
        _pr = get_recipe(persona_recipe)
        if _pr is not None:
            return Route(recipe=_pr,
                         tools=filter_schemas(all_schemas, frozenset(_pr.tools)),
                         temperature=0.2,
                         reason=f"persona:{_pr.id}")

    recipe = match_recipe(text or "")
    if recipe is not None:
        return Route(recipe=recipe,
                     tools=filter_schemas(all_schemas, frozenset(recipe.tools)),
                     temperature=0.2,
                     reason=f"recipe:{recipe.id}")
    # 兜底路径服务的是「没有配方管这个」的任意输入（含闲聊/问价）——
    # 采集类工具（fallback=False）一律不放进去，否则模型会在闲聊里自由写库。
    fallback_tools = _fallback_tools(all_schemas)
    if is_stock_question(text or ""):
        return Route(recipe=None, tools=fallback_tools, temperature=0.2,
                     reason="stock_question")
    return Route(recipe=None, tools=fallback_tools, temperature=0.5, reason="default")


def _fallback_tools(all_schemas: list[dict]) -> list[dict]:
    """全量兜底用的工具集：剔除 `fallback=False` 的采集类工具。

    注册表不可用时退回原样（宁可按旧行为跑，也不能因为一个过滤器把工具集清空）。
    """
    try:
        from src.agents.chat.registry import no_fallback_tool_names, schema_tool_name

        excluded = no_fallback_tool_names()
    except Exception:  # noqa: BLE001 - 路由不许因为注册表导入失败而炸
        return list(all_schemas)
    if not excluded:
        return list(all_schemas)
    return [s for s in all_schemas if schema_tool_name(s) not in excluded]
