"""请求级 Harness 上下文：@提及解析、配方注入、工具白名单与预算。

抽到 `src/harness/` 而不是留在 `chat.py` 里，有两个原因：
  1. `tests/test_w33_chat_registry.py::test_chat_py_line_shrink` 给 chat.py 定了行数上限
     （该文件历史上膨胀到 3000+ 行），新逻辑必须外置；
  2. 这层是"怎么驾驭模型"而不是"HTTP 接口"，跟配方/评测同属 harness 地基。
"""
from __future__ import annotations

import re
from contextvars import ContextVar

from pydantic import BaseModel

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


def bind_symbol(db, conv, symbol: str, market: str | None = None) -> None:
    """把标的写回会话(落库失败只回滚, 不抛 —— 上下文不该把对话搞挂)。"""
    conv.stock_symbol = symbol
    conv.stock_market = market or _market_of(symbol)
    try:
        db.commit()
    except Exception:
        db.rollback()


def sync_conv_symbol(db, conv, body) -> None:
    """把本轮该聊的标的绑到对话上(页面切换时跟随更新)。

    判定见 resolve_bound_symbol; 只在真的变了时才落库, 避免每条消息多一次 commit。
    """
    target = resolve_bound_symbol(
        getattr(conv, "stock_symbol", None),
        getattr(body, "stock_symbol", None),
        getattr(body, "content", "") or "",
        mention=mention_stock(body),
    )
    if not target or target == (getattr(conv, "stock_symbol", None) or "").strip():
        return
    anchor = (getattr(body, "stock_symbol", None) or "").strip()
    market = resolve_market(target, anchor, getattr(body, "stock_market", None))
    bind_symbol(db, conv, target, market)


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

    route = route_for_request(text, all_schemas, forced)
    if stock_q and route.recipe is None:
        p, txt = detect_persona(db, text, bound)
        # 只有"尺子会量错"的三类才接管：妖股/左侧/题材龙头。
        # 趋势/常规交回模型自选 —— 否则每只正常票都会被塞进 10 工具管线，慢且没必要。
        if p is not None and getattr(p, "kind", "") in ("demon", "left", "leader"):
            route = route_for_request(text, all_schemas, forced, persona_recipe=p.recipe)
            return route, txt
        # 判断类问题（能不能拿/洗盘还是出货/该不该卖）必须有证据才能答。
        # 关键词表里没有这些词时，旧行为是「给模型全量工具让它自己选」——实测它会一个都不调，
        # 最后触发零工具拒答。这里确定性兜底到全景诊断（10 个工具、六段输出）。
        if _is_judgment_question(text):
            route = route_for_request(text, all_schemas, forced, persona_recipe="deep_diagnosis")
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

        today = beijing_now().strftime("%Y-%m-%d")
        uid = str(getattr(user, "id", "") or "")
        q = db.query(AnalysisHistory).filter(
            AnalysisHistory.agent_name == "premarket_outlook",
            AnalysisHistory.status == "success",
        )
        if uid:
            q = q.filter(or_(AnalysisHistory.user_id == uid, AnalysisHistory.user_id.is_(None)))
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


def enter_request_ctx(db, conv, body, messages: list[dict]):
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
    return [
        (REQ_BOUND, REQ_BOUND.set(bound)),
        (REQ_TEXT, REQ_TEXT.set(getattr(body, "content", "") or "")),
        (REQ_TOOLS, REQ_TOOLS.set(frozenset(recipe.tools) if recipe and recipe.tools else None)),
        (REQ_BUDGET, REQ_BUDGET.set({"max": recipe.max_tool_calls, "used": 0} if recipe else None)),
        (REQ_RECIPE_LABEL, REQ_RECIPE_LABEL.set(recipe.label if recipe else "")),
    ]


def exit_request_ctx(tokens) -> None:
    for var, tok in reversed(tokens):
        var.reset(tok)
