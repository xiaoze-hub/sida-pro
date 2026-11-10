"""请求级路由（2026-10-21）：把"用哪些工具、什么温度"从模型手里拿回来。

实测依据（8 问题 × 3 次，数据冻结的午休时段）：
- 同一问题工具集合 Jaccard 0.52；最差「全景深度诊断」三次 4 / 0 / 3 个工具；
- 耗时 CV 0.55（同一问题 20s ~ 117s）。
方差主要来自"模型自选工具 + 0.5 温度"。这个模块只做两件确定性的事：
1. 命中配方关键词 → 工具 schema 收紧到配方白名单（运行时还有 REQ_TOOLS 兜底拒绝）；
2. 数据类问题温度降到 0.2（抄数字不需要创造力）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Route:
    recipe: object | None = None
    tools: list[dict] = field(default_factory=list)
    temperature: float = 0.5
    reason: str = "default"


# 「要一批标的」的市场意图词（2026-11-10 复审 P0）。
# 这些词出现时，会话里绑着某只票**不代表**用户在问那只票：
# 截图原句「今天尾盘有适合建仓的股票吗，可以有创业板」在 bound=002361 的会话里发出，
# 旧实现见 bound 就判「有标的」，`尾盘` 抓到的单标的 `intraday_analyst` 压过了
# `建仓` 的 `entry_scanner`，用户拿到「我是分时解读员，只能看个股分时」。
# 判定顺序：正文写了代码/核对得到的简称 > 市场意图 > bound > 无标的。
_MARKET_INTENT_WORDS = (
    "市场", "全市场", "两市", "大盘", "选股", "扫一遍", "扫描",
    "有哪些股票", "有什么股票", "哪些票", "有什么票", "买什么", "买哪只",
    "建仓", "埋伏", "候选", "涨幅榜", "跌幅榜", "涨停", "跌停", "热点",
    "题材", "轮动", "板块", "龙虎榜", "复盘",
)

# 「我的持仓」类问题：必须有 `get_portfolio` 才可能答对，
# bound 里的那只票不是用户问的对象（见 `recipes.match_recipe_for` 的持仓护栏）。
_PORTFOLIO_WORDS = ("我的持仓", "持仓", "仓位", "加仓", "减仓", "调仓", "自选")


def is_market_intent(text: str) -> bool:
    t = text or ""
    return any(w in t for w in _MARKET_INTENT_WORDS)


def is_portfolio_intent(text: str) -> bool:
    t = text or ""
    return any(w in t for w in _PORTFOLIO_WORDS)


_NAME_MISS_TTL_S = 120.0
_NAME_MISS: dict[str, float] = {}


def _lookup_name(word: str):
    """名称核对 + 120s 负缓存：查不到的词不每轮重探（TQ 探测预算 3s 起）。"""
    import time as _time

    now = _time.time()
    if _NAME_MISS.get(word, 0.0) > now - _NAME_MISS_TTL_S:
        return []
    from src.core.symbol_guard import _match_cached

    hits = _match_cached(word)
    if not hits:
        _NAME_MISS[word] = now
    return hits


def _verified_symbol_mention(text: str) -> bool:
    """正文里是否真的点了标的：6 位代码，或**核对得到**的简称（不猜）。

    简称核对走 `symbol_guard._match_cached` —— 它在请求链路上本来就会跑
    （`enter_request_ctx` 的 `first_code_from_names`），10 分钟缓存，不新增一类网络请求。
    核对不上（无此票 / 行情源不可用）一律当「没有标的」：宁可把市场问题交给市场配方，
    也不能拿「创业板」「半导体科技」这种词去当票查。
    """
    try:
        from src.core.symbol_guard import _CODE_RE, mention_candidates

        if _CODE_RE.search(text or ""):
            return True
        for word in mention_candidates(text or ""):
            if _lookup_name(word):
                return True
    except Exception:  # noqa: BLE001 - 判定失败按"没有标的"处理，不许打断路由
        return False
    return False


def has_symbol_scope(text: str, bound: str | None = None) -> bool:
    """这句话是不是在问**某只具体标的**（决定 symbol / market 口径的配方谁能赢）。

    顺序即语义（2026-11-10 复审 P0）：
    1. 正文有代码 / 核对得到的简称 → 单标的（用户明确点了票，bound 与市场词都不改道）；
    2. 有市场意图词 → 市场（bound 是会话状态，不是这句问句的口径）；
    3. 才轮到 bound。
    """
    if _verified_symbol_mention(text or ""):
        return True
    # 持仓问句问的是「我这一篮子」，bound 只是会话状态；市场问句同理。
    if is_portfolio_intent(text or "") or is_market_intent(text or ""):
        return False
    return bool(bound)


def route_for_request(text: str, all_schemas: list[dict],
                      forced: frozenset[str] | set[str] | None = None,
                      persona_recipe: str | None = None,
                      bound: str | None = None) -> Route:
    """决定本轮工具集与温度。

    优先级：@提及白名单 > 标的画像（persona_recipe）> 关键词路由 > 默认。
    画像是确定性判定（stock_persona.detect），比关键词更可靠 ——
    妖股用「趋势+估值」的配方分析必然输出废话，所以画像命中时直接锁定配方。

    `bound`（会话锁定标的）参与路由：关键词命中里优先选**与问句口径一致**的配方
    （有标的 → symbol，无标的 → market），见 `recipes.match_recipe_for`。
    2026-11-10：「今天尾盘有适合建仓的股票吗」被 `尾盘` 抓到单标的 `intraday_analyst`，
    管线解析不到标的就 `None`，用户最后拿到一句"我是分时解读员，不能扫描"。
    """
    from src.harness.recipes import filter_schemas, get_recipe, match_recipe_for
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

    recipe = match_recipe_for(text or "", has_symbol=has_symbol_scope(text or "", bound))
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

    注册表不可用时退回原样（宁可按旧行为跑，也不能因为一个过滤器把工具集清空）——
    但这条 fail-open 必须留痕：它意味着写库类工具可能会被放回闲聊兜底集。
    """
    try:
        from src.agents.chat.registry import no_fallback_tool_names, schema_tool_name

        excluded = no_fallback_tool_names()
    except Exception as exc:  # noqa: BLE001 - 路由不许因为注册表导入失败而炸
        logger.warning(
            "兜底工具集过滤失效，本轮按全量下发（含 fallback=False 的写库工具）: %s", exc
        )
        return list(all_schemas)
    if not excluded:
        return list(all_schemas)
    return [s for s in all_schemas if schema_tool_name(s) not in excluded]
