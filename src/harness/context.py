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
        conv.stock_symbol = code
        conv.stock_market = getattr(body, "stock_market", None) or _market_of(code)
        try:
            db.commit()
        except Exception:
            db.rollback()

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


def enter_request_ctx(db, conv, body, messages: list[dict]):
    """装配本轮上下文，返回需要回滚的 token 列表。"""
    recipe, note = apply_mentions(db, conv, body)
    inject_recipe(messages, recipe, note)
    bound = getattr(conv, "stock_symbol", None) or extract_code(getattr(body, "content", "") or "")
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
