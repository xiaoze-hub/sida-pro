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
# 判定顺序：正文代码 > 市场/持仓意图 > bound > 名字像票但核不到 > 无标的。
#
# **路由层不发任何网络请求**（2026-11-10 复审阻断）：上一版在这里调
# `symbol_guard._match_cached` 核对简称，TQ 单次探测预算 3s，直接在请求链路上
# 阻塞事件循环，源故障时还给出不确定结论。名字解析本来就在上游
# （`enter_request_ctx` → `first_code_from_names`）做过，结果体现在 `bound` 里；
# 这里只做本地正则判断，核不到就**不猜**。
_MARKET_INTENT_WORDS = (
    "市场", "全市场", "两市", "大盘", "选股", "扫一遍", "扫描",
    "有哪些股票", "有什么股票", "哪些票", "有什么票", "买什么", "买哪只",
    "建仓", "埋伏", "候选", "涨幅榜", "跌幅榜", "涨停", "跌停", "热点",
    "题材", "轮动", "板块", "龙虎榜", "复盘",
    # 板块/指数名不是标的：会话绑着 002361 时问「创业板今天怎么样」问的还是板块，
    # 绑定不许把它拽成那只票（2026-11-10 复审：无代码的市场问句避免绑定劫持）。
    "创业板", "科创板", "北交所", "主板", "指数", "概念股",
)

# 「我的持仓」类问题：必须有 `get_portfolio` 才可能答对，
# bound 里的那只票不是用户问的对象（见 `recipes.match_recipe_for` 的持仓护栏）。
_PORTFOLIO_WORDS = ("我的持仓", "持仓", "仓位", "加仓", "减仓", "调仓", "自选")

# 「举例代码」的判定词（2026-11-10 复审 P1）：`有没有像 600111 这样的票` 里的
# 600111 是**例子**，不是提问对象。三项同时成立才算举例，缺一不可：
#   a. 代码前/后紧邻举例框架（比如/例如/类似/像 … 这样/这种/这类/之类）；
#   b. 正文里有"一批"的名词（票/股票/标的/个股/品种）；
#   c. 问句在要一批（市场意图词，或 有没有/哪些/哪几/还有什么/其他）。
# 只靠 a 会把「300750 这样的走势还能追吗」误判成市场问句 —— 那是在问那只票。
_EXAMPLE_BEFORE = ("比如", "例如", "譬如", "类似", "好比", "诸如", "像", "如")
_EXAMPLE_AFTER = ("这样", "这种", "这类", "之类", "那种", "那样")
_BATCH_NOUNS = ("股票", "标的", "个股", "品种", "票")
_BATCH_ASKS = ("有没有", "哪些", "哪几", "还有什么", "其他", "另外", "几只")
#: 单数指代：「这票有没有放量」问的是当前那一只，不是要一批。
_SINGULAR_MARKERS = ("这票", "这只", "那只", "该股", "该票", "此股", "这家")


def is_market_intent(text: str) -> bool:
    t = text or ""
    return any(w in t for w in _MARKET_INTENT_WORDS)


def is_portfolio_intent(text: str) -> bool:
    t = text or ""
    return any(w in t for w in _PORTFOLIO_WORDS)


def _asks_for_batch(text: str) -> bool:
    """问句是不是在要一批标的。

    判据只两条，都要落在字面上：市场意图词（"复盘/建仓/板块…"），
    或者「复数问法 + 批量名词」（"有没有…的票"，"哪些股票"）。
    只靠复数问法会把「分时有没有放量」这种绑定单标的的问句判成市场问句。
    """
    t = text or ""
    if is_market_intent(t):
        return True
    if any(w in t for w in _SINGULAR_MARKERS):
        return False
    return any(n in t for n in _BATCH_NOUNS) and any(w in t for w in _BATCH_ASKS)


def _code_spans(text: str) -> list[tuple[int, int]]:
    """正文里 6 位 A 股代码的 (起, 止) 位置。纯本地正则，不联网、不猜。"""
    try:
        from src.core.symbol_guard import _CODE_RE
    except Exception:  # noqa: BLE001 - 护栏模块炸了就当没有代码，不打断路由
        return []
    return [(m.start(), m.end()) for m in _CODE_RE.finditer(text or "")]


def _is_example_code(text: str, start: int, end: int) -> bool:
    """这个代码是不是被当**例子**写的（`像 600111 这样的票`）。

    判定只用明确的举例框架 + 批量名词 + 复数问法，不做语义猜测；
    任一条不成立就按"点了这只票"处理（宁可多认一只标的，也不把单标的问句改成市场）。
    """
    t = text or ""
    before = t[max(0, start - 4):start].rstrip(" \t\u3000，,：:（(")
    after = t[end:end + 5].lstrip(" \t\u3000，,：:（(")
    framed = any(before.endswith(w) for w in _EXAMPLE_BEFORE) or any(
        after.startswith(w) for w in _EXAMPLE_AFTER)
    if not framed:
        return False
    if not any(n in t for n in _BATCH_NOUNS):
        return False
    return _asks_for_batch(t)


def _name_like_mention(text: str) -> bool:
    """正文里像点了某只票的名字（后缀词），**不核对**（核不了就不猜是哪只）。

    这一层只用来决定"别拿市场配方代答"，绝不据此认定是哪只票：
    真正的名字→代码解析在上游 `enter_request_ctx`，结果走 `bound`。
    """
    try:
        from src.core.symbol_guard import mention_candidates

        return bool(mention_candidates(text or ""))
    except Exception:  # noqa: BLE001
        return False


def has_symbol_scope(text: str, bound: str | None = None) -> bool:
    """这句话是不是在问**某只具体标的**（决定 symbol / market 口径的配方谁能赢）。

    顺序即语义（2026-11-10 复审 P0，全程本地判断、不发网络请求）：

    1. 正文写了 6 位代码**且不是在举例** → 单标的（用户明确点了票，bound 与市场词都不改道）；
       `有没有像 600111 这样的票` 里的代码是例子，按市场口径（2026-11-10 复审 P1）；
    2. 持仓/市场意图词 → 市场（bound 是会话状态，不是这句问句的口径）；
    3. 才轮到 bound（会话绑定 / 上游名字解析的结果）；
    4. 都不是，但正文像点了股票名 → 仍按单标的处置：核不到是哪只就交给上游核对/追问，
       绝不改成市场配方拿全市场名单**代答**。
    """
    t = text or ""
    spans = _code_spans(t)
    if spans and any(not _is_example_code(t, s, e) for s, e in spans):
        return True
    # 持仓问句问的是「我这一篮子」，市场问句要的是一批标的；bound 只是会话状态。
    # 「正文的代码全是举例」（`有没有像 600111 这样的票`）同样按市场处置：
    # 否则会话绑定会把这类"要一批"的问句拽成那只示例票（2026-11-10 复审 P1）。
    example_only = bool(spans) and all(_is_example_code(t, s, e) for s, e in spans)
    if is_portfolio_intent(t) or _asks_for_batch(t) or example_only:
        return False
    if bound:
        return True
    return _name_like_mention(t)


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
    # 口径如实标出来（2026-11-10 复审 P1）：`有没有像 600111 这样的票` 是市场问句，
    # 不能因为正文里有个示例代码就被日志/治理页记成「单标的问句」。
    if has_symbol_scope(text or "", bound):
        return Route(recipe=None, tools=fallback_tools, temperature=0.2,
                     reason="stock_question")
    if _asks_for_batch(text or ""):
        return Route(recipe=None, tools=fallback_tools, temperature=0.2,
                     reason="market_question")
    # 最后一档听护栏：`has_symbol_scope` 只认正文代码/绑定这类**字面证据**，而
    # 「这句话是不是在问某只票」的权威判定一直是 `symbol_guard.is_stock_question`
    # （`context`/`chat` 两处都按它决定要不要先取数）。上一版把这一档整体换成
    # `has_symbol_scope`，护栏判「是单股问句」的输入就掉进 `default`：口径被记成
    # 闲聊、温度 0.5（单股问句是数据题，必须 0.2）。
    # 顺序不能提前：护栏对举例代码（`有没有像 600111 这样的票`）同样返回 True，
    # 那类问句的口径是市场，先被上面的市场分支接走，不许在这里被拽回单标的。
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
