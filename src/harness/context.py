"""请求级 Harness 上下文：@提及解析、配方注入、工具白名单与预算。

抽到 `src/harness/` 而不是留在 `chat.py` 里，有两个原因：
  1. `tests/test_w33_chat_registry.py::test_chat_py_line_shrink` 给 chat.py 定了行数上限
     （该文件历史上膨胀到 3000+ 行），新逻辑必须外置；
  2. 这层是"怎么驾驭模型"而不是"HTTP 接口"，跟配方/评测同属 harness 地基。
"""
from __future__ import annotations

import asyncio
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# 本轮锁定标的（对话绑定或用户点/写的代码）
REQ_BOUND: ContextVar[str | None] = ContextVar("chat_req_bound", default=None)
# 本轮用户原文（用于判断"用户有没有明确指定代码"）
REQ_TEXT: ContextVar[str] = ContextVar("chat_req_text", default="")
# 本轮工具白名单（内置智能体配方）；None = 不限制
REQ_TOOLS: ContextVar[frozenset[str] | None] = ContextVar("chat_req_tools", default=None)
# 本轮工具预算 {"max": int, "used": int}
REQ_BUDGET: ContextVar[dict | None] = ContextVar("chat_req_budget", default=None)
# 本轮智能体展示名（写进拒绝文案，让用户知道是谁在限制）
REQ_RECIPE_LABEL: ContextVar[str] = ContextVar("chat_req_recipe", default="")
# 本轮页面视图状态（`/copilot/context` 上报的那份；2026-10-28 P2 类型链）。
# 为什么放进请求上下文：标注提议要知道"用户在看的图是个股还是指数"（000001 两重身份），
# 而这份信息只在 `_build_ai_messages` 里拿得到，管线在另一层调用 —— 用 contextvar 传，
# 比把 dict 沿 4 层函数签名逐个加参数要稳（漏传一次就退回"猜类型"）。
REQ_VIEW: ContextVar[dict | None] = ContextVar("chat_req_view", default=None)
# 本轮图表快照引用（`SendMessageBody.chart_snapshot` 经服务端复核后的那一份）。
# 为什么是请求上下文：工具在 registry 里按名字分发，那条路径拿不到 HTTP 请求体；
# 而这份快照**只对这一条消息有效** —— 不许入会话 initial_context、不许写库、
# 不许跨轮复用（模型/客户端下一轮不带就是没有，见 prepare_chart_snapshot）。
REQ_CHAN_SNAPSHOT: ContextVar[dict | None] = ContextVar("chat_req_chan_snapshot", default=None)
# 本轮请求标的的**类型**（stock/index）。同号个股/指数（000001）只有这一维，
# 工具层用它做最后一层归属核对；判不出来就是 None（不是"大概是股票"）。
REQ_BOUND_INSTRUMENT: ContextVar[str | None] = ContextVar("chat_req_bound_instrument", default=None)

_CODE_RE = re.compile(r"(?<!\d)([036489]\d{5})(?!\d)")


class Mention(BaseModel):
    """@ 提及。stock → 绑定标的；agent → 选一套 harness 配方。"""

    type: str          # "stock" | "agent"
    value: str         # 6 位代码 或 recipe id
    label: str | None = None


def _market_of(code: str) -> str:
    return "SH" if code[0] == "6" else ("BJ" if code[0] in "489" else "SZ")


# ── 标的绑定判定（2026-10-22「页面上下文串入」事故）────────────────────────────
# 事故: 用户在 002548 页面聊完, 再到 002114 页面提问, 参谋仍按 002548 作答 ——
# 旧实现"对话已绑定就不改", 页面锚点永远追不上, 系统提示与取数上下文全串在旧票上。
# 判定口径(与前端 AiCopilot 的解析顺序一致):
#     文字里的 6 位代码 > @提及 > 页面锚点(body.stock_symbol) > 会话旧绑定


def _is_code(value: str | None) -> bool:
    """是否合法的 6 位 A 股代码。"""
    v = (value or "").strip()
    return len(v) == 6 and v.isdigit()


def mention_stock(body) -> str | None:
    """取 @ 提及里的股票代码(没有/非法则 None)。"""
    for m in (getattr(body, "mentions", None) or []):
        if (getattr(m, "type", "") or "").lower() != "stock":
            continue
        v = (getattr(m, "value", "") or "").strip()
        if _is_code(v):
            return v
    return None


def resolve_market(symbol: str, anchor: str | None = None, market: str | None = None) -> str:
    """标的的市场。

    页面锚点就是这只票(或没有锚点)时用前端带来的市场; 其余按代码前缀推断,
    与前端 marketOf 同口径(6→SH / 0,3→SZ / 4,8,9→BJ)。
    """
    m = (market or "").strip()
    a = (anchor or "").strip()
    if m and (not a or a == symbol):
        return m
    return _market_of(symbol)


def resolve_bound_symbol(conv_symbol: str | None, body_symbol: str | None,
                         text: str = "", mention: str | None = None) -> str | None:
    """本轮对话该锁定哪只票(返回 None = 不锁定)。判定, 不落库。

    优先级: 文字里的 6 位代码 > @提及 > 页面锚点 > 会话旧绑定。
    - 文字里写了代码 → 用它(用户明说的永远优先; 与既有行为一致)
    - @提及 → 用它(显式动作, 优先于页面)
    - 页面锚点: 合法 6 位码且与旧绑定不同 → 用它(页面切换, 跟随更新)
    - 否则保留旧绑定(没有就都不锁)
    """
    conv_symbol = (conv_symbol or "").strip()
    body_symbol = (body_symbol or "").strip()
    code = extract_code(text or "")
    if code:
        return code
    mention = (mention or "").strip()
    if mention:
        return mention
    if _is_code(body_symbol) and body_symbol != conv_symbol:
        return body_symbol
    return conv_symbol or None


@dataclass(frozen=True)
class TurnTarget:
    """本轮请求目标（最终口径）。

    为什么要有这一层: 旧实现里「本轮该聊哪只票」有两个不一致的答案 ——
    ``plan_conv_symbol`` 只认「文字代码 / @提及 / 页面锚点 / 会话绑定」，
    而正文只写名字（「北方稀土 缠论结构怎么样」）时要等 ``enter_request_ctx``
    才解析成 600111。于是快照归属校验拿不到名字解析结果，**无绑定会话 = 快照可信**，
    同一轮里 REQ_BOUND=600111 而可信快照是 000002 也能出他票结构（2026-11-17 审计反例）。

    ``source`` 记录目标从哪来（"text"/"mention"/"anchor"/"conversation"/"name"），
    只用于日志与文案；``problem`` 非空表示**判不出唯一目标**，调用方必须 fail-closed。
    """

    symbol: str | None = None
    market: str | None = None
    source: str = "none"
    #: "no_target"（既无显式标的也没绑定）/ "unresolved_name" / "ambiguous_name"
    #: / "name_unverifiable"（一次都没确认出名字, 而整句又不全是泛指/追问用语 —— 名称识别
    #:   覆盖不到整句, 就不能声称"正文没点别的票"）
    #: / "ambiguous_target"（正文给了不止一个标的: 多个代码、代码与 @提及 各指一只）
    #: / "name_conflict"（正文名字与显式标的/锚点/会话绑定指向不同的票）
    problem: str | None = None
    #: 正文里的名字解析出的代码（仅用于说明冲突, 不参与取数）
    name_symbol: str | None = None
    #: 本目标的**标的类型**（stock/index）。同号个股/指数靠它区分，没有它就不能
    #: 把任何快照当成本轮可信来源（见 prepare_chart_snapshot）。
    instrument: str | None = None
    #: 类型的依据: "code"（仅凭代码可证）/ "page"（本轮页面显式声明）/
    #: "view"（服务端缓存的页面视图状态）。只用于日志与文案，不是"证明"。
    instrument_source: str | None = None


def resolve_explicit_target(conv, body) -> TurnTarget | None:
    """只认**显式**标的（不查网络）: 文字代码 > @提及 > 页面锚点 > 会话绑定。"""
    text = getattr(body, "content", "") or ""
    anchor = (getattr(body, "stock_symbol", None) or "").strip()
    conv_symbol = (getattr(conv, "stock_symbol", None) or "").strip()

    code = extract_code(text)
    if code:
        return TurnTarget(code, resolve_market(code, anchor, getattr(body, "stock_market", None)),
                          "text")
    mention = mention_stock(body)
    if mention:
        return TurnTarget(mention, resolve_market(mention, anchor, getattr(body, "stock_market", None)),
                          "mention")
    if _is_code(anchor) and anchor != conv_symbol:
        return TurnTarget(anchor, resolve_market(anchor, anchor, getattr(body, "stock_market", None)),
                          "anchor")
    if conv_symbol:
        # 市场口径与 plan_conv_symbol 完全一致(锚点为空/等于该票时用前端带来的市场)。
        return TurnTarget(
            conv_symbol,
            resolve_market(conv_symbol, anchor, getattr(body, "stock_market", None)),
            "conversation",
        )
    return None


def _name_target(text: str) -> tuple[str | None, str | None]:
    """正文里的股票名 → (代码, 问题)。**旁路**: 任何异常都当"解析不出"。

    「解析模糊/失败一律 fail-closed」是这里的关键: 名字命中却对不到唯一代码、
    或一句话里出现多只不同名的票, 都不能拿快照硬凑 —— 宁可本轮不给结构。

    **「没认出名字」也不等于「正文没点别的票」**（2026-11-17 五轮审计）: 本地名单只覆盖
    全名（4/3 字 + 句首两字简称），TQ 兜底只探每段开头 3~4 字，所以段中简称、过期/残缺
    名单里的全名都可能漏检（`coverage` 是 "local"/"tq"/"none" 都一样）。因此这条路只在
    **正向可证**时才允许"这句话只指当前页面": 整句都由泛指/追问用语构成
    （`unverified_name_words` 为空）；否则返回 ``name_unverifiable`` 让上层整份丢弃快照。
    """
    try:
        from src.core.symbol_guard import (
            bare_name_codes, is_generic_reference, is_stock_question, resolve_mentions,
            unverified_name_words, whole_text_name_codes,
        )

        if not text:
            return None, None
        info = resolve_mentions(text) if is_stock_question(text) else {}
        resolved = [(w, c) for (w, c, _n) in (info.get("resolved") or [])]
        unmapped = [w for (w, *_rest) in (info.get("suggestions") or [])]
        unmapped += [w for w in (info.get("unknown") or [])]
        # 泛指眼前这只（「这家银行」「整个医药」）不是点名: 当"没检测到名字"，
        # 让上层继续按页面锚点/会话绑定走，而不是把一句正常追问判成"解析失败"。
        resolved = [(w, c) for (w, c) in resolved if not is_generic_reference(w)]
        unmapped = [w for w in unmapped if not is_generic_reference(w)]
        if not resolved and not unmapped:
            # 没有后缀词的名字（「贵州茅台」）：补一次整段确认，只认行情库真的命中的。
            resolved = [(w, c) for (w, c, _n) in bare_name_codes(text)]
        if not resolved and not unmapped:
            # 紧贴提问词、没有分隔符（「贵州茅台缠论怎么样」）：整段窗口确认。
            # 注意：这里的空结果只说明**这次没探到**，不是"正文没点名" —— 覆盖边界
            # （段中名称、简称、残缺名单）见 symbol_guard 模块头。
            hits, _coverage = whole_text_name_codes(text)
            resolved = [(w, c) for (w, c, _n) in hits]
        if not resolved and not unmapped:
            # 一次检测都没确认出名字。两种可能：用户真没点名，或者名字被漏检了 ——
            # 后者无法排除，所以**只能**用正向证据放行：整句都由泛指/追问用语构成。
            # 只要句子里还有认不出的词（可能是简称/错别字/新名称），就不能拿页面上的
            # 旧图顶上（「我想看看贵州茅台缠论怎么样」= 浦发结构答给茅台）。
            unknown = unverified_name_words(text)
            if unknown:
                logger.info(
                    "名称识别覆盖不到整句(本地名单只认全名/句首, TQ 只探段首), "
                    "无法确认正文没点别的票 → 本轮按无唯一目标处理 unknown=%s",
                    unknown[:3],
                )
                return None, "name_unverifiable"
            return None, None            # 整句都是泛指/追问：交给上层按当前页面用
        # 已经确认出一只名字，也不代表句子只有这一只：「浦发银行还是贵州茅台」里第一遍
        # 只按后缀词命中了「浦发银行」，剩下那半句还藏着没后缀的票。把已确认的名字词
        # 抹掉，对**剩余正文**再跑一次整段确认；剩下那半句仍认不出时同样不能放行。
        rest = text
        for word, _code in resolved:
            if word:
                rest = rest.replace(word, " ")
        if not unmapped and rest.strip():
            hits, _ = whole_text_name_codes(rest)
            for word, code, _n in hits:
                if code not in {c for _w, c in resolved}:
                    resolved.append((word, code))
            if len({c for _w, c in resolved}) == 1:
                unknown = unverified_name_words(rest)
                if unknown:
                    logger.info(
                        "正文去掉已确认的名字后仍有认不出的表述, 无法排除第二只标的 → "
                        "本轮按无唯一目标处理 unknown=%s", unknown[:3],
                    )
                    return None, "name_unverifiable"
        codes: list[str] = []
        for _word, code in resolved:
            code6 = str(code or "").split(".")[0].strip()
            if _is_code(code6) and code6 not in codes:
                codes.append(code6)
    except Exception:  # noqa: BLE001 - 名字解析是旁路, 失败等于没解析出来
        logger.info("本轮标的名称解析失败(已按缺失处理)", exc_info=True)
        return None, "unresolved_name"
    if len(codes) > 1:
        return None, "ambiguous_name"
    if not codes:
        # 确实点了名却对不到可分析的 A 股代码:
        #   查不到(错别字/已退市/只有相似建议) 或 命中的是基金/ETF/港股等本链路不支持的
        #   标的 —— 两种都不许拿锚点那张图顶替。
        return None, "unresolved_name"
    if unmapped:
        # 一句话里还有名字对不上（「浦发银行和某某科技」）: 判不出唯一目标, 不许只按
        # 对上的那只算 —— 否则另一只票的问题会被这张图"顶替"答掉。
        return None, "unresolved_name"
    return codes[0], None


# ── 标的类型（stock/index）证据链 ──────────────────────────────────────────
# 2026-11-17 三轮审计缺口: `ChatConversation` 只存 `stock_symbol`（可选 `stock_market`），
# 快照归属三层（消息入口 / 开流后 / 工具）过去只比 6 位代码。于是会话绑 000001
# （平安银行）时，随附的「上证指数 000001」（instrument=index，摘要完全合法）会被当成
# 本轮那张图，指数结构被说成"你问的那只票"的结构。
#
# 类型只认三种来源，**绝不从代码猜**：
#   ① `provable_instrument`：仅凭代码可证（600519 只可能是个股、399001 只可能是指数）；
#   ② 本轮请求里对**页面锚点**的显式声明（`body.stock_instrument`，且锚点就是它）；
#   ③ 服务端缓存的页面视图状态（`/copilot/context` 上报的 symbol+instrument）。
# 三者都没有 ⇒ 本轮没有可证的标的身份 ⇒ 快照整份丢弃（同号场景明确 fail-closed）。
# ②③ 都只是**客户端对"我在看哪张图"的陈述**，服务端比对一致性后使用，不等于证明了主数据。

_INSTRUMENTS = ("stock", "index")


def _provable_instrument(symbol: str, market: str | None) -> str | None:
    """仅凭代码能证明的标的类型；证明不了返回 None（不猜）。旁路失败 = 不可证。"""
    try:
        from src.harness.actions import provable_instrument

        mk = (market or "").strip().upper()
        return provable_instrument(symbol, mk if mk in ("CN", "SH", "SZ", "BJ") else "CN")
    except Exception:  # noqa: BLE001 - 类型判定是旁路, 失败等于不可证
        logger.info("标的类型代码证明失败(按不可证处理) symbol=%s", symbol, exc_info=True)
        return None


def _page_instrument(body, symbol: str) -> str | None:
    """本轮请求对**页面锚点类型**的声明（锚点不是这只票时不算数）。"""
    raw = str(getattr(body, "stock_instrument", None) or "").strip().lower()
    if raw not in _INSTRUMENTS:
        return None
    anchor = (getattr(body, "stock_symbol", None) or "").strip()
    if anchor and anchor != symbol:
        return None                      # 声明说的是另一只票的图, 与本轮目标无关
    return raw


def _view_instrument(conv, symbol: str) -> str | None:
    """服务端缓存的页面视图状态里的类型（必须同一代码，否则不算）。"""
    try:
        from src.harness.viewstate import load_view_state

        uid = getattr(conv, "user_id", None)
        if not uid:
            return None
        # 会话级优先；工作台是整页上报（不带 conversation_id），再回退用户级那条。
        for conv_id in (getattr(conv, "id", None), None):
            state = load_view_state(uid, conv_id) or {}
            if str(state.get("symbol") or "").strip() != symbol:
                continue
            inst = str(state.get("instrument") or "").strip().lower()
            if inst in _INSTRUMENTS:
                return inst
    except Exception:  # noqa: BLE001 - 视图状态是旁路
        return None
    return None


def _instrument_evidence(conv, body, symbol: str,
                         market: str | None = None) -> tuple[str | None, str | None]:
    """本轮目标的 (类型, 依据)。没有可信依据就返回 (None, None) —— 调用方必须 fail-closed。"""
    proof = _provable_instrument(symbol, market)
    if proof:
        return proof, "code"             # 代码可证时优先: 页面声明不能把 600000 说成指数
    declared = _page_instrument(body, symbol)
    if declared:
        return declared, "page"
    cached = _view_instrument(conv, symbol)
    if cached:
        return cached, "view"
    return None, None


def _second_target_in_text(body, explicit: TurnTarget) -> tuple[str, str | None] | None:
    """显式标的之外，正文里是否还给了**第二个**标的（代码或名字）。旁路，异常=不判定。

    为什么要查（2026-11-17 五轮审计）：「用户写了代码」只证明**那一个**标的，不证明
    正文没有第二只。旧实现对 ``source in ("text", "mention")`` 直接返回显式标的，
    于是「600000 和 600519 哪个缠论更好」「600000 我想看看贵州茅台缠论怎么样」都拿着
    600000 的快照作答 —— 正是"用代码存在忽略第二标的"。

    返回 (problem, name_symbol)；None = 正文没有第二个标的。
    """
    symbol = (explicit.symbol or "").strip()
    text = getattr(body, "content", "") or ""
    try:
        others = [c for c in _CODE_RE.findall(text) if c != symbol]
        if others:
            return "ambiguous_target", None
        if explicit.source == "text":
            mention = mention_stock(body)
            if mention and mention != symbol:
                return "ambiguous_target", None     # 文字代码与 @提及 各指一只
        # 名字检测在出现 6 位代码时会短路（不许改用户明写的标的），所以先剥掉代码，
        # 再对**剩下的正文**做同一套判定：命中别的票 → name_conflict；还有认不出的词 →
        # name_unverifiable（不能因为给了代码就宣称"正文没有第二只"）。
        residual = _CODE_RE.sub(" ", text)
        code, problem = _name_target(residual)
    except Exception:  # noqa: BLE001 - 判定旁路：异常当"没发现第二标的"，不改变显式路径
        logger.info("显式标的的第二标的判定失败(按未发现处理)", exc_info=True)
        return None
    if problem:
        return problem, None
    if code and code != symbol:
        return "name_conflict", code
    return None


def resolve_turn_target(conv, body, *, resolve_names: bool = False) -> TurnTarget:
    """本轮请求目标的**最终**口径（显式标的优先，必要时才做名字解析）。

    ``resolve_names=False`` 时与 ``plan_conv_symbol`` 同口径（零网络调用）: 只有
    携带图表快照的轮次才值得为"名字"多打一次 TQ —— 不带快照的轮次行为完全不变。

    显式标的（文字代码/@提及）**永远优先**；但当目标来自页面锚点/会话绑定、而正文里又
    点名了**另一只**股票时，属于"本轮到底在聊哪只"自相矛盾 —— 一样 fail-closed，
    否则用户问北方稀土、页面停在 600000 时，快照会把 600000 的结构答成他问的那只。

    显式标的也**不是免检**（``_second_target_in_text``）：一句里两个代码、或代码之外
    还点了别的名字，同样判不出唯一目标 —— "写了代码"只证明那一个标的。

    正文**确实点了名**却解析不出唯一代码（错别字/查不到/一句里还有对不上的名字）时，
    页面锚点与会话绑定都不能替用户决定本轮标的：2026-11-17 三轮审计实测，
    旧实现在这条分支直接 `return explicit`，于是「图为 600000 + 正文写了别的名字」把
    600000 的快照当成了本轮那张图。现在这条与 `ambiguous_name` 同样丢弃。
    """
    explicit = resolve_explicit_target(conv, body)
    if explicit is not None:
        instrument, instrument_source = _instrument_evidence(
            conv, body, explicit.symbol or "", explicit.market,
        )
        explicit = replace(explicit, instrument=instrument,
                           instrument_source=instrument_source)
        if not resolve_names:
            return explicit
        second = _second_target_in_text(body, explicit)
        if second is not None:
            problem, name_symbol = second
            return TurnTarget(symbol=explicit.symbol, market=explicit.market,
                              source=explicit.source, problem=problem,
                              name_symbol=name_symbol, instrument=explicit.instrument,
                              instrument_source=explicit.instrument_source)
        if explicit.source in ("text", "mention"):
            return explicit
        code, problem = _name_target(getattr(body, "content", "") or "")
        if problem and problem != "ambiguous_name":
            # 名字解析失败 / 无法核对: 锚点/绑定都只是"上一轮留下的位置", 不是本轮
            # 标的的证据。**新问题类型必须走这条**, 不许因为没写 if 就滑到 `return explicit`。
            return TurnTarget(problem=problem, name_symbol=code)
        if problem == "ambiguous_name":
            return TurnTarget(symbol=explicit.symbol, market=explicit.market,
                              source=explicit.source, problem=problem,
                              instrument=explicit.instrument,
                              instrument_source=explicit.instrument_source)
        if code and code != explicit.symbol:
            return TurnTarget(symbol=explicit.symbol, market=explicit.market,
                              source=explicit.source, problem="name_conflict",
                              name_symbol=code, instrument=explicit.instrument,
                              instrument_source=explicit.instrument_source)
        return explicit
    if not resolve_names:
        return TurnTarget(problem="no_target")
    text = getattr(body, "content", "") or ""
    code, problem = _name_target(text)
    if problem:
        return TurnTarget(problem=problem)
    if code:
        market = _market_of(code)
        instrument, instrument_source = _instrument_evidence(conv, body, code, market)
        return TurnTarget(code, market, "name", instrument=instrument,
                          instrument_source=instrument_source)
    return TurnTarget(problem="no_target")


def bind_symbol(db, conv, symbol: str, market: str | None = None) -> None:
    """把标的写回会话(落库失败只回滚, 不抛 —— 上下文不该把对话搞挂)。"""
    conv.stock_symbol = symbol
    conv.stock_market = market or _market_of(symbol)
    try:
        db.commit()
    except Exception:
        db.rollback()


def plan_conv_symbol(conv, body) -> tuple[str | None, str | None]:
    """本轮该绑定的 (symbol, market)。**纯判定，不落库**。

    快照校验需要知道"本轮到底在聊哪只票"，但校验本身可能失败(403/409) ——
    那条消息按契约是"拒绝且未保存"，所以校验之前**绝不能**先把标的写进会话。
    """
    target = resolve_bound_symbol(
        getattr(conv, "stock_symbol", None),
        getattr(body, "stock_symbol", None),
        getattr(body, "content", "") or "",
        mention=mention_stock(body),
    )
    if not target:
        return None, None
    anchor = (getattr(body, "stock_symbol", None) or "").strip()
    return target, resolve_market(target, anchor, getattr(body, "stock_market", None))


def sync_conv_symbol(db, conv, body) -> None:
    """把本轮该聊的标的绑到对话上(页面切换时跟随更新)。

    判定见 resolve_bound_symbol; 只在真的变了时才落库, 避免每条消息多一次 commit。
    """
    target, market = plan_conv_symbol(conv, body)
    if not target or target == (getattr(conv, "stock_symbol", None) or "").strip():
        return
    bind_symbol(db, conv, target, market)


def current_bound_symbol(db, conversation_id: int) -> str | None:
    """库里**此刻**的会话标的(绕开 ORM 身份映射, 用于落消息前的最后核对)。"""
    from src.db.models import ChatConversation

    row = (db.query(ChatConversation.stock_symbol)
           .filter(ChatConversation.id == conversation_id).first())
    return ((row[0] if row else None) or "").strip() or None


def apply_mentions(db, conv, body):
    """解析 @ 提及：股票 → 绑标的；智能体 → 载入配方。返回 (recipe|None, note|None)。"""
    from src.harness.recipes import get_recipe

    mentions = getattr(body, "mentions", None) or []
    stocks = [m.value.strip() for m in mentions
              if (m.type or "").lower() == "stock" and re.fullmatch(r"\d{6}", (m.value or "").strip())]
    agents = [m.value.strip() for m in mentions if (m.type or "").lower() == "agent"]

    note = None
    if stocks:
        code = stocks[0]
        if len(stocks) > 1:
            note = f"（本轮最多支持 1 只标的，已使用 {code}）"
        # 文字里写了 6 位代码时以文字为准(与 resolve_bound_symbol 同口径):
        # 否则系统提示(按文字)与工具锁定(按 @提及)会指向不同的票。
        if extract_code(getattr(body, "content", "") or "") is None:
            anchor = (getattr(body, "stock_symbol", None) or "").strip()
            market = resolve_market(code, anchor, getattr(body, "stock_market", None))
            if ((getattr(conv, "stock_symbol", None) or "") != code
                    or (getattr(conv, "stock_market", None) or "") != market):
                bind_symbol(db, conv, code, market)

    recipe = None
    for r in agents:
        recipe = get_recipe(r)
        if recipe:
            break
    return recipe, note


def inject_recipe(messages: list[dict], recipe, note: str | None = None) -> None:
    """把配方片段拼进 system 消息（只影响本轮，不改库里的 SYSTEM_PROMPT）。"""
    if not messages:
        return
    extra = []
    if recipe is not None:
        extra.append(f"--- 本轮由用户指定的内置智能体：{recipe.label}（{recipe.id}） ---")
        if recipe.prompt_fragment:
            extra.append(recipe.prompt_fragment)
        if recipe.output_sections:
            extra.append("输出段落顺序：" + " → ".join(recipe.output_sections))
        if recipe.tools:
            extra.append("本轮只允许调用这些工具：" + "、".join(recipe.tools))
        try:
            from src.harness.skills import skills_block

            manual = skills_block(getattr(recipe, "skills", ()))
            if manual:
                extra.append("--- 研判手册（按其顺序读，不要跳步） ---\n" + manual)
        except Exception:  # noqa: BLE001 - 手册是增强, 缺了不影响作答
            pass
    if note:
        extra.append(note)
    if not extra:
        return
    first = messages[0]
    if first.get("role") == "system":
        first["content"] = (first.get("content") or "") + "\n\n" + "\n".join(extra)
    else:
        messages.insert(0, {"role": "system", "content": "\n".join(extra)})


def extract_code(text: str) -> str | None:
    m = _CODE_RE.search(text or "")
    return m.group(1) if m else None


# 诊断类意图词：只有"要我判断"这类问题才值得按画像锁配方。
# 单纯问价（"北方稀土 35.03 元，-0.57%"、"600111 多少钱"）不该触发 12 个工具的管线。
_DIAGNOSIS_WORDS = (
    "怎么样", "诊断", "分析", "看看", "看一下", "研判", "点评", "如何", "能不能",
    "值不值", "值得", "风险", "机会", "预期", "后续", "怎么办", "还能", "该不该",
)


def is_diagnosis_intent(text: str) -> bool:
    """是否属于"要我给判断"的问题（决定要不要按标的画像锁配方）。"""
    t = text or ""
    return any(w in t for w in _DIAGNOSIS_WORDS)


def route_with_persona(db, text: str, bound: str | None, stock_q: bool,
                      all_schemas: list[dict], forced=None):
    """路由 + 标的画像合一，返回 (Route, 画像文本)。

    画像只在「没有命中具体配方 + 是诊断类问题」时接管：单纯问价不该触发 12 工具管线。
    逻辑放这里而不是 chat.py —— chat.py 有 2300 行护栏。
    """
    from src.harness.routing import route_for_request

    # `bound` 进路由：关键词命中里优先与问句口径一致的那份（有标的→symbol / 无标的→market）
    route = route_for_request(text, all_schemas, forced, bound=bound)
    if stock_q and route.recipe is None:
        p, txt = detect_persona(db, text, bound)
        # 只有"尺子会量错"的三类才接管：妖股/左侧/题材龙头。
        # 趋势/常规交回模型自选 —— 否则每只正常票都会被塞进 10 工具管线，慢且没必要。
        if p is not None and getattr(p, "kind", "") in ("demon", "left", "leader"):
            route = route_for_request(text, all_schemas, forced, persona_recipe=p.recipe, bound=bound)
            return route, txt
        # 判断类问题（能不能拿/洗盘还是出货/该不该卖）必须有证据才能答。
        # 关键词表里没有这些词时，旧行为是「给模型全量工具让它自己选」——实测它会一个都不调，
        # 最后触发零工具拒答。这里确定性兜底到全景诊断（10 个工具、六段输出）。
        if _is_judgment_question(text):
            route = route_for_request(text, all_schemas, forced,
                                      persona_recipe="deep_diagnosis", bound=bound)
            return route, txt
    return route, ""


# 判断类问题：用户在要一个"该不该/是洗盘还是出货"的结论，而不是问价格。
_JUDGMENT_WORDS = (
    "还能拿", "还能持有", "持有吗", "还能买", "该不该", "要不要卖", "要不要买",
    "止盈", "止损", "洗盘", "出货", "吸筹", "派发", "见顶", "见底", "割肉", "加仓", "减仓",
)


def _is_judgment_question(text: str) -> bool:
    t = text or ""
    return any(w in t for w in _JUDGMENT_WORDS)


def bind_recipe_trace(runtime, recipe) -> None:
    """把本轮命中的配方写进轨迹。

    2026-10-21 实测：165 次运行里 164 次 `copilot_runs.recipe` 为 NULL —— 轨迹在路由之前创建，
    配方之后才确定却从没回写，治理页因此看不到「哪个画像/配方被用了」。
    """
    trace = getattr(runtime, "trace", None)
    if trace is None or recipe is None:
        return
    try:
        trace.set_recipe(getattr(recipe, "id", None) or str(recipe))
    except Exception:  # noqa: BLE001  埋点失败不该影响作答
        pass


def detect_persona(db, text: str, bound: str | None):
    """标的画像旁路（2026-10-21）。

    先判「这是妖股 / 左侧超跌 / 题材龙头 / 趋势白马 / 常规」，再由画像锁配方：
    拿趋势+估值的尺子量五连板妖股，输出必然是「跌破支撑位、建议观望」这种废话。
    失败一律 (None, "")，不影响主流程；失败原因只记 info。

    放在 context.py 而不是 chat.py：chat.py 有行数护栏，画像逻辑属于上下文层。
    """
    if not is_diagnosis_intent(text or ""):
        return None, ""
    try:
        from src.core.stock_persona import detect, render

        sym = (bound or extract_code(text or "") or "").split(".")[0].strip()
        if not sym:
            return None, ""
        _p = detect(db, sym)
        return _p, render(_p)
    except Exception:  # noqa: BLE001  画像只是旁路，任何异常都不该影响作答
        return None, ""


def _load_user_of_conv(db, conv):
    """取会话归属用户(盘前底稿注入用)。取不到返回 None —— 旁路, 不影响作答。"""
    uid = getattr(conv, "user_id", None)
    if not uid:
        return None
    try:
        from src.db.models import User

        return db.query(User).filter(User.id == uid).first()
    except Exception:  # noqa: BLE001
        return None


def _append_system_note(messages: list[dict], note: str) -> None:
    """把旁路说明并入第一个 system 消息; 没有 system 就置顶插一条。"""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            base = m.get("content") or ""
            m["content"] = f"{base}\n\n{note}" if base else note
            return
    messages.insert(0, {"role": "system", "content": note})


def inject_premarket_brief(messages: list[dict], db, user, recipe=None) -> bool:
    """把该用户最新盘前分析底稿注入 system 消息(盘前策略官配方自动调用, 单测可直接调)。

    - 无记录 / 无用户 / 正文空 / 异常 → 不注入(返回 False), 旁路绝不影响主流程;
    - 当日(analysis_date == 今日) → 注入「【今日盘前分析底稿（{date}）】」;
    - 历史日期 → 显式标注「【历史盘前分析底稿（基准日 {date}，非今日，仅供对照）】」,
      绝不许把昨日记录冒充今日。
    """
    if not messages or db is None or user is None:
        return False
    try:
        from sqlalchemy import or_

        from src.core.timezone import beijing_now
        from src.db.models import AnalysisHistory
        from src.core.analysis_history import analysis_history_scope_clause

        today = beijing_now().strftime("%Y-%m-%d")
        uid = str(getattr(user, "id", "") or "")
        q = db.query(AnalysisHistory).filter(
            AnalysisHistory.agent_name == "premarket_outlook",
            AnalysisHistory.status == "success",
            # 2026-10-29 五审: NULL 历史底稿只给 owner —— 正文可能是别人的持仓,
            # 旧写法把它当"共享"注入任何用户的 system prompt。
            analysis_history_scope_clause(uid, getattr(user, "role", None)),
        )
        rec = (
            q.order_by(
                AnalysisHistory.analysis_date.desc(),
                AnalysisHistory.created_at.desc(),
                AnalysisHistory.id.desc(),
            )
            .first()
        )
        if rec is None:
            return False
        content = (rec.content or "").strip()
        if not content:
            return False
        d = str(rec.analysis_date)
        if d == today:
            brief = f"【今日盘前分析底稿（{d}）】\n{content}"
        else:
            brief = (
                f"【历史盘前分析底稿（基准日 {d}，非今日，仅供对照）】\n{content}\n"
                f"（注: 该底稿基准日为 {d}，非今日，只作历史对照，勿当作今日盘前结论。）"
            )
        _append_system_note(messages, brief)
        return True
    except Exception:  # noqa: BLE001  注入是旁路, 任何异常都不该影响作答
        return False


# ── 本轮图表快照（缠论结构工具的"同图"来源）──────────────────────────────────
# 事故形态: 模型/客户端会把上一轮、甚至别只票的 `chan_snapshot.digest` 塞进工具参数,
# 算出的分型就被当成"用户这张图"引用。旧实现只能靠提示词禁止; 现在改成链路约束:
#   客户端在**本条消息**上显式声明 chart_snapshot(严格 schema) → 消息入口用**既有只读
#   端点**复核"摘要与此刻这份 K 线一致" → 只放进本轮 ContextVar → 工具以它为准
#   (覆盖模型自带的 digest), 模型给别的 symbol 则拒绝; 下一轮不带就是没有。
# 诚实边界: 这仍是一份**客户端声明**。服务端只证明了"摘要与服务器此刻这份 K 线一致",
# 没有、也不可能证明"用户屏幕上真的显示了这张图" —— 日志与文案都不许写成"已核对屏幕"。


class ChartSnapshotBody(BaseModel):
    """`SendMessageBody.chart_snapshot` 的严格 schema（多一个字段 / 类型不对 → 422）。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    #: 摘要方案号。换版本即证明"不是同一套字节流", 旧值必须被拒, 不能被兼容。
    schema_name: Literal["sida-chan-snapshot-v2"] = Field(alias="schema")
    symbol: str = Field(pattern=r"^\d{6}$")
    market: Literal["CN"] = "CN"
    instrument: Literal["stock", "index"]
    interval: Literal["1d"] = "1d"
    days: int = Field(ge=1, le=2000)
    #: 完整 64 位小写十六进制; 截短/大写一律拒绝(与端点 422 口径同源, 不"自动修正")。
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class TurnChartSnapshot:
    """一条消息的图表快照结论：`ref` = 通过复核的可信来源，`note` = 显式缺失说明。

    ``turn_symbol`` 是**本轮请求目标**（名字解析也在内）: 即便快照被丢弃也会带上,
    让后续 ``enter_request_ctx``/工具复用同一次判定, 不必二次解析(也就不多打一次 TQ),
    同时让"服务端认为这轮在聊哪只票"与"快照属于哪只票"永远只有一个答案。

    ``turn_instrument`` 是同一目标的**类型**（stock/index）: 同号个股/指数只有它能区分,
    开流后复核（``recheck_turn_snapshot``）与工具层都按它再核一次。
    """

    ref: dict | None = None
    note: str | None = None
    turn_symbol: str | None = None
    turn_instrument: str | None = None


def _chart_snapshot_note(ref: dict, instrument_source: str | None = None) -> str:
    """本轮快照注入 system 的说明（只拼进本轮消息, 绝不写回库里 prompt/历史）。

    摘要只给**前 8 位**: 模型不需要、也不该拿到完整摘要 —— 取数一律以服务端本轮复核
    结果为准(见 tools_chan), 于是这份 64hex 不必出现在可能被回显/落库的地方。

    文案的时间限定（2026-11-17 三轮审计）: 复核发生在**消息入口那一刻**，不是"此刻仍然
    一致"。之后 K 线若变化，工具侧会以 409 如实拒绝 —— 不许把这句读成长期有效保证。
    """
    from src.harness.redaction import short_digest

    basis = {
        "code": "类型由代码证明",
        "page": "类型来自本轮页面的显式声明(不是代码证明)",
        "view": "类型来自服务端缓存的页面视图状态(不是代码证明)",
    }.get(str(instrument_source or ""), "")
    basis_line = f"{basis}\n" if basis else ""
    return (
        "【本轮图表快照（消息入口时已复核）】\n"
        f"symbol={ref['symbol']} market={ref['market']} instrument={ref['instrument']} "
        f"interval={ref['interval']} days={ref['days']} "
        f"as_of_day={ref.get('as_of_day') or '未知'}\n"
        f"snapshot_digest={short_digest(ref['digest'])}（只展示前 8 位; 完整摘要由服务端本轮持有,\n"
        "不需要也不要向用户索取或复述）\n"
        f"{basis_line}"
        "这是本条消息随附的客户端声明: 消息入口那一刻服务端只用同一取数链重算摘要确认了"
        "「内容一致」, 之后 K 线若有更新, 工具会以 409/失败如实拒绝, 不要把这句当成"
        "「此刻仍然一致」。服务端"
        "没有、也不能验证用户屏幕。调用 get_chan_structure 时就用上面这组参数(工具以本轮"
        "复核结果为准), 不要改写 digest、不要另取新快照、不要向用户再要摘要。"
    )


def _require_chart_snapshot_user(db, user) -> None:
    """携带图表快照的前提：真实用户身份 + 行情权限（与 HTTP API 同源口径）。"""
    from fastapi import HTTPException

    if user is None or not getattr(user, "id", None) or getattr(user, "is_service", False):
        raise HTTPException(
            status_code=403,
            detail="图表快照只能由已登录的真实用户携带；当前通道没有可核验的用户，本条消息已拒绝。",
        )
    from src.agents.chat.registry import _perm_denied
    from src.core.permissions import PERM_VIEW_QUOTE

    denied = _perm_denied(user, db, PERM_VIEW_QUOTE)
    if denied:
        raise HTTPException(status_code=403, detail=f"无权携带图表快照：{denied}")


def _no_target_note(problem: str, snapshot_symbol: str, *,
                    target_symbol: str | None = None,
                    name_symbol: str | None = None) -> str:
    """判不出唯一请求目标时的显式说明（fail-closed, 不给结构）。"""
    why = {
        "unresolved_name": "正文里的标的名没能解析成唯一代码(可能是错别字/已退市/只有相似建议)",
        "name_unverifiable": (
            "服务端无法确认正文有没有点名别的标的 —— 名称索引只覆盖一部分写法(本地名单只认"
            "全名: 4 字/3 字与句首两字简称; 行情库兜底只探每段开头 3~4 字), 简称、句中的"
            "名称、新上市名称都可能漏检, 所以「没认出名字」不等于「没提别的票」"
        ),
        "ambiguous_name": "正文里出现了多只不同标的, 无法确定这张图属于哪一只",
        "ambiguous_target": (
            "正文里给了不止一个标的(多个代码, 或代码与 @提及 各指一只), "
            "无法确定这张图属于哪一只"
        ),
        "name_conflict": (
            f"正文里的标的名指向 {name_symbol or '另一只票'}, 与当前标的"
            f" {target_symbol or '未知'} 不是同一只, 本轮目标自相矛盾"
        ),
    }.get(problem, "既没有显式标的(代码/@提及/页面锚点), 也没有可用的会话绑定")
    return (
        f"（本轮随附的图表快照绑定 {snapshot_symbol}, 但本轮请求标的无法唯一确定: {why}。"
        "整份快照已丢弃, 本轮不提供缠论结构 —— 请用 6 位代码或 @提及 指明标的,"
        "或先在该标的图表页提问。不要把「没有可信归属」当成「这张图没有结构」。）"
    )


def _instrument_unprovable_note(symbol: str, snapshot_instrument: str) -> str:
    """同号个股/指数且没有可信类型声明时的显式说明（fail-closed, 不给结构）。"""
    return (
        f"（本轮随附的图表快照绑定 {symbol}({snapshot_instrument}), 但服务端**无法确认本轮"
        f"请求标的的类型**: {symbol} 既可能是个股也可能是指数, 代码本身不能证明, "
        "本轮也没有该标的的页面类型声明。同号不同标的不是同一张图, 整份快照已丢弃 —— "
        "本轮不提供缠论结构, 也不得按代码猜成个股或指数。"
        "请在该标的的图表页(个股图/指数图)提问, 或在提问时带上页面类型; "
        "不要把「没有可信归属」当成「这张图没有结构」。）"
    )


def _instrument_conflict_note(symbol: str, target_instrument: str,
                              snapshot_instrument: str) -> str:
    """目标类型与快照类型不一致时的显式说明（同号个股/指数）。"""
    return (
        f"（本轮请求标的是 {symbol}({target_instrument}), 而随附的图表快照是 "
        f"{symbol}({snapshot_instrument}): 同号个股/指数不是同一张图, "
        "整份快照已丢弃 —— 本轮不提供缠论结构, 也不得引用旧摘要或另取新快照。"
        "若用户确实在看另一类图, 请在该标的对应的图表页重新提问。）"
    )


async def prepare_chart_snapshot(db, conv, body, user,
                                 *, turn_target: TurnTarget | None = None) -> TurnChartSnapshot | None:
    """校验本轮 `body.chart_snapshot`；返回本条消息可用的可信来源（None = 没带）。

    ``turn_target`` 给"本轮请求目标"的**只读判定结果**（``resolve_turn_target``）；
    不传时在这里补算（含名字解析，仅本轮带快照时才会发生）。**必须**在 bind 之前完成 ——
    403/409 的消息按契约"拒绝且未保存"，不许先 commit 一个标的再拒绝。

    硬拒绝（抛 HTTPException：本条消息不落库、不回答）:
      * 403 没有真实用户身份（服务令牌/无 id）或没有行情权限;
      * 409 摘要与当前 K 线内容不一致（图表已变/跨 worker 缓存不一致/同码标的/旧摘要）——
        **绝不自行刷新或另取一张新快照冒充"用户这张图"**。
    软丢弃（返回 note：本轮严格缺失，聊天继续）:
      * 标的不一致 —— 本轮请求目标（含名字解析出的代码）与快照标的不是同一只;
      * 判不出唯一目标（正文只写名字但解析失败/一句话含多只票/既无显式标的也无绑定/
        正文有名称识别覆盖不到的表述 —— 见 `name_unverifiable`）——
        **fail-closed**: 没有可信归属就不给结构，不许把"无绑定"当成"可信";
      * 显式代码之外还点了别的标的（多个代码 / 代码与 @提及 各指一只 / 代码 + 别的名字）——
        "写了代码"只证明那一个标的，不证明正文没有第二只;
      * 422 该快照当前不满足分型前提; 503/上游异常（校验不了就明说没拿到）。
    """
    declared = getattr(body, "chart_snapshot", None)
    if declared is None:
        return None
    from fastapi import HTTPException

    _require_chart_snapshot_user(db, user)

    if turn_target is None:
        turn_target = resolve_turn_target(conv, body, resolve_names=True)
    if turn_target.problem:
        logger.info(
            "本轮图表快照无唯一请求目标 user=%s snapshot=%s problem=%s",
            getattr(user, "id", None), declared.symbol, turn_target.problem,
        )
        return TurnChartSnapshot(
            turn_symbol=turn_target.symbol or None,
            note=_no_target_note(turn_target.problem, declared.symbol,
                                 target_symbol=turn_target.symbol,
                                 name_symbol=turn_target.name_symbol),
        )
    target = (turn_target.symbol or "").strip()
    if target != declared.symbol:
        logger.info(
            "本轮图表快照标的与请求目标冲突 user=%s target=%s snapshot=%s source=%s",
            getattr(user, "id", None), target, declared.symbol, turn_target.source,
        )
        return TurnChartSnapshot(turn_symbol=target or None, note=(
            f"（本轮随附的图表快照绑定 {declared.symbol}，与当前请求标的 {target} 不一致，"
            "整份快照已丢弃：本轮不提供缠论结构，也不得引用旧摘要或另取新快照。）"
        ))

    # 同号个股/指数: 代码相同 ≠ 同一张图。类型只认代码证明/页面声明/视图状态,
    # 一样都没有就丢（不猜），对不上也丢。
    target_instrument = (turn_target.instrument or "").strip().lower()
    if not target_instrument:
        logger.info(
            "本轮图表快照缺少可证的标的类型 user=%s symbol=%s snapshot_instrument=%s source=%s",
            getattr(user, "id", None), declared.symbol, declared.instrument,
            turn_target.instrument_source,
        )
        return TurnChartSnapshot(
            turn_symbol=target or None,
            note=_instrument_unprovable_note(declared.symbol, declared.instrument),
        )
    if target_instrument != declared.instrument:
        logger.info(
            "本轮图表快照类型与请求目标冲突 user=%s symbol=%s target=%s snapshot=%s source=%s",
            getattr(user, "id", None), declared.symbol, target_instrument,
            declared.instrument, turn_target.instrument_source,
        )
        return TurnChartSnapshot(
            turn_symbol=target or None, turn_instrument=target_instrument,
            note=_instrument_conflict_note(declared.symbol, target_instrument,
                                           declared.instrument),
        )

    # 复核只走**既有只读端点**（同一份 get_klines + 同一套摘要算法），不另写一份校验。
    from src.web.api.chan_snapshot import get_chan_structure as _endpoint

    try:
        payload = await asyncio.to_thread(
            _endpoint,
            symbol=declared.symbol,
            snapshot=declared.digest,
            instrument=declared.instrument,
            market=declared.market,
            interval=declared.interval,
            days=declared.days,
            _=user,
        )
    except HTTPException as exc:
        status = int(exc.status_code)
        logger.info(
            "本轮图表快照复核未通过 user=%s symbol=%s status=%s",
            getattr(user, "id", None), declared.symbol, status,
        )
        if status == 409:
            raise HTTPException(
                status_code=409,
                detail=(
                    "图表快照已过期或与当前K线内容不一致（服务端重算摘要不符），本条消息已拒绝且未保存。"
                    "服务端不会自行刷新、也不会另取一张快照冒充当前图；请刷新图表后重发。"
                ),
            ) from None
        return TurnChartSnapshot(turn_symbol=target or None, note=(
            f"（本轮图表快照未通过复核(status={status})，已丢弃：本轮不提供缠论结构，"
            "也不得把「没拿到」写成「这张图没有结构」。）"
        ))
    except Exception as exc:  # noqa: BLE001 - 详情只进日志, 不回显给模型/用户
        logger.warning(
            "本轮图表快照复核异常 user=%s symbol=%s error=%s",
            getattr(user, "id", None), declared.symbol, type(exc).__name__,
        )
        return TurnChartSnapshot(turn_symbol=target or None, note=(
            "（本轮图表快照无法复核（上游异常），已丢弃：本轮不提供缠论结构，"
            "不得据此推断这张图没有分型/笔。）"
        ))

    if not isinstance(payload, dict) or payload.get("snapshot_digest") != declared.digest:
        logger.warning(
            "本轮图表快照复核结果异常 user=%s symbol=%s",
            getattr(user, "id", None), declared.symbol,
        )
        return TurnChartSnapshot(turn_symbol=target or None, note=(
            "（本轮图表快照复核结果与声明不一致，已丢弃：本轮不提供缠论结构。）"
        ))

    ref = {
        "symbol": declared.symbol,
        "market": declared.market,
        "instrument": declared.instrument,
        "interval": declared.interval,
        "days": declared.days,
        "digest": declared.digest,
        "as_of_day": payload.get("as_of_day"),
    }
    logger.info(
        "本轮图表快照已复核 user=%s symbol=%s instrument=%s days=%s as_of=%s",
        getattr(user, "id", None), ref["symbol"], ref["instrument"], ref["days"],
        ref["as_of_day"],
    )
    return TurnChartSnapshot(ref=ref, note=_chart_snapshot_note(ref, turn_target.instrument_source),
                             turn_symbol=target, turn_instrument=target_instrument)


async def preflight_chart_snapshot(conversation_id: int, body, user) -> TurnChartSnapshot | None:
    """流式入口的**开流前**校验（SSE 一旦开始就改不了 HTTP 状态码, 409/403 要真 409）。

    本条消息没带 chart_snapshot 时零成本返回 None（不查库、不取数）。
    会话不存在时不在这里报错: 交给开流后的既有"对话不存在"错误事件, 不改语义。

    这里**只做判定不落库**: 本轮标的用 resolve_turn_target(含名字解析)推出来给校验当参照,
    拒绝时(403/409)会话仍然是原来的 A —— 不会出现"报错却已经把会话改成 B"。
    真正的 bind 留到 gen 里、消息确实要保存时(见 `_bind_conv_symbol`)。
    """
    if getattr(body, "chart_snapshot", None) is None:
        return None
    from src.db.models import ChatConversation
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        _require_chart_snapshot_user(db, user)
        conv = (
            db.query(ChatConversation)
            .filter(
                ChatConversation.id == conversation_id,
                ChatConversation.user_id == user.id,
            )
            .first()
        )
        if conv is None:
            return None
        # 纯判定: 校验失败不留痕。带快照的轮次才做名字解析(无快照路径零网络调用)。
        target = resolve_turn_target(conv, body, resolve_names=True)
        return await prepare_chart_snapshot(db, conv, body, user, turn_target=target)
    finally:
        db.close()


def recheck_turn_snapshot(snapshot: TurnChartSnapshot | None,
                          bound_symbol: str | None) -> TurnChartSnapshot | None:
    """落消息/调工具前的**最后一道**核对: 用此刻的会话标的复核快照归属。

    preflight 通过 ≠ 现在仍然成立: 开流到真正取库之间, 另一轮可能已经把会话切到了
    别的票(TOCTOU), 客户端带来的声明也不是"已验证的屏幕"。所以这里再核一次标的,
    不一致就整份丢弃 —— SSE 已经开始, 改不了状态码; 与"切标的"同一软丢弃口径。

    两个方向都要核: ① 库里的会话此刻是否还指向这份快照的标的; ② 本轮判定出的
    ``turn_symbol``(含名字解析)是否就是快照标的 —— 只看 ① 会漏掉"会话无绑定但正文
    写的是另一只票"这种形态。

    第三个方向是**类型**（2026-11-17 三轮审计）: 同号个股/指数(000001)只看代码会互相
    顶替, 所以 ``turn_instrument`` 与快照自述 instrument 也必须一致; 判不出类型
    （``turn_instrument`` 为空）说明消息入口就没证成身份 —— 这份快照不该在这里。
    """
    if not isinstance(snapshot, TurnChartSnapshot) or snapshot.ref is None:
        return snapshot
    bound = (bound_symbol or "").strip()
    symbol = (snapshot.ref.get("symbol") or "").strip()
    turn = (snapshot.turn_symbol or "").strip()
    turn_inst = (snapshot.turn_instrument or "").strip().lower()
    snap_inst = (snapshot.ref.get("instrument") or "").strip().lower()
    instrument_drift = bool(not turn_inst or (snap_inst and turn_inst != snap_inst))
    if (not symbol or (bound and bound != symbol) or (turn and turn != symbol)
            or instrument_drift):
        logger.info(
            "本轮图表快照开流后漂移 bound=%s turn=%s snapshot=%s turn_instrument=%s "
            "snapshot_instrument=%s", bound, turn, symbol, turn_inst or "-", snap_inst or "-",
        )
        return TurnChartSnapshot(turn_symbol=turn or bound or None, note=(
            f"（本轮随附的图表快照绑定 {symbol}（{snap_inst or '未知类型'}），与本轮请求标的 "
            f"{turn or bound}（{turn_inst or '类型未证实'}）不一致，"
            "整份快照已丢弃：本轮不提供缠论结构，也不得引用旧摘要或另取新快照。）"
        ))
    return snapshot


def enter_request_ctx(db, conv, body, messages: list[dict],
                      chart_snapshot: TurnChartSnapshot | None = None):
    """装配本轮上下文，返回需要回滚的 token 列表。"""
    recipe, note = apply_mentions(db, conv, body)
    inject_recipe(messages, recipe, note)
    # 盘前策略官配方(2026-10-23 B 支线): 注入该用户最新盘前底稿;
    # 历史记录会被显式标注「非今日」, 绝不许把昨日底稿冒充今日。
    if recipe is not None and getattr(recipe, "id", "") == "premarket_strategist":
        _user = _load_user_of_conv(db, conv)
        if _user is not None:
            inject_premarket_brief(messages, db, _user, recipe)
    _body_text = getattr(body, "content", "") or ""
    bound = getattr(conv, "stock_symbol", None) or extract_code(_body_text)
    # 名字解析已经在快照校验里做过一次(带快照的轮次), 这里直接复用结论:
    # 既省一次 TQ(17709 单线程), 也保证"服务端认为这轮在聊哪只票"与快照归属同源。
    if not bound and isinstance(chart_snapshot, TurnChartSnapshot):
        bound = chart_snapshot.turn_symbol or None
    if not bound:
        # 正文只写名字（「北方稀土还能持有吗」）→ 先把名字解析成代码,
        # 否则 bound 为空会让画像判定与配方路由统统落空（实测导致零工具拒答）。
        try:
            from src.core.symbol_guard import first_code_from_names, is_stock_question

            # 只有像在问个股才去查名字 —— 否则「今天大盘怎么样」也会白打一次 TQ
            # (17709 是单线程, 每轮对话都多打一次不可接受)。
            if is_stock_question(_body_text):
                bound = first_code_from_names(_body_text)
        except Exception:  # noqa: BLE001  旁路, 解析不出就当没有
            bound = None
    # 本轮图表快照: 只进本条消息的 system 与本轮 contextvar, 不入库、不跨轮复用。
    # 必须**无条件 set**(不带的轮次也 set None), 否则租户内复用任务时可能读到上一轮的值。
    _snap_ref = None
    if isinstance(chart_snapshot, TurnChartSnapshot):
        _snap_ref = chart_snapshot.ref
        if chart_snapshot.note:
            _append_system_note(messages, chart_snapshot.note)
    return [
        (REQ_BOUND, REQ_BOUND.set(bound)),
        (REQ_BOUND_INSTRUMENT, REQ_BOUND_INSTRUMENT.set(
            (chart_snapshot.turn_instrument or None)
            if isinstance(chart_snapshot, TurnChartSnapshot) else None)),
        (REQ_TEXT, REQ_TEXT.set(getattr(body, "content", "") or "")),
        (REQ_TOOLS, REQ_TOOLS.set(frozenset(recipe.tools) if recipe and recipe.tools else None)),
        (REQ_BUDGET, REQ_BUDGET.set({"max": recipe.max_tool_calls, "used": 0} if recipe else None)),
        (REQ_RECIPE_LABEL, REQ_RECIPE_LABEL.set(recipe.label if recipe else "")),
        (REQ_CHAN_SNAPSHOT, REQ_CHAN_SNAPSHOT.set(_snap_ref)),
    ]


def exit_request_ctx(tokens) -> None:
    for var, tok in reversed(tokens):
        var.reset(tok)
