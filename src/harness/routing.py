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
                      forced: frozenset[str] | set[str] | None = None) -> Route:
    """决定本轮工具集与温度。forced 是 @提及 指定的白名单（优先于自动路由）。"""
    from src.harness.recipes import filter_schemas, match_recipe
    from src.core.symbol_guard import is_stock_question

    if forced:
        return Route(recipe=None, tools=filter_schemas(all_schemas, forced),
                     temperature=0.2 if is_stock_question(text) else 0.3,
                     reason="mention")

    recipe = match_recipe(text or "")
    if recipe is not None:
        return Route(recipe=recipe,
                     tools=filter_schemas(all_schemas, frozenset(recipe.tools)),
                     temperature=0.2,
                     reason=f"recipe:{recipe.id}")
    if is_stock_question(text or ""):
        return Route(recipe=None, tools=list(all_schemas), temperature=0.2,
                     reason="stock_question")
    return Route(recipe=None, tools=list(all_schemas), temperature=0.5, reason="default")
