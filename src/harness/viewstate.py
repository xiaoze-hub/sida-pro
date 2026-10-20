"""页面即上下文(2026-10-20, P1)。

问题: 用户在 K 线页面盯着 600111 的 30 分钟图圈了一段区间, 切到参谋还得打一遍
「600111 最近这段为什么跌」——AI 不知道"这段"是哪段, 只能反问或猜。

做法: 前端把当前视图状态(POST /api/copilot/context)推给后端, 后端按会话缓存
30 分钟, 提问时作为一段**紧凑上下文**注入(≤300 字, 不抢 K 线口径的位置)。

优先级铁律(与标的锁定一致):
    用户本条文字里的代码 > @提及 > 会话绑定 > 页面锚点
页面锚点**只补空**, 绝不覆盖用户明说的标的。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from src.web.cache.biz_cache import biz_cache

TTL_SECONDS = 1800
_KEY = "biz:copilot:view:{user}:{conv}"


class ViewState(BaseModel):
    """前端当前视图。字段全部可选——推不全也不该报错。"""

    page: str | None = None                 # kline / dashboard / workbench / copilot
    symbol: str | None = None
    market: str | None = None
    period: str | None = None               # 1d / 30m / 5m
    rng: dict[str, str] | None = Field(default=None, alias="range")
    selection: dict[str, Any] | None = None  # 圈选区间 {from,to} 或 {start,end}
    indicators: list[str] | None = None
    anchor_line: float | None = None         # 光标所在价位

    model_config = {"populate_by_name": True}


def _key(user_id: Any, conversation_id: int | None) -> str:
    return _KEY.format(user=str(user_id or "-"), conv=conversation_id or 0)


def save_view_state(user_id: Any, conversation_id: int | None, state: ViewState) -> bool:
    try:
        return bool(biz_cache.set_json(
            _key(user_id, conversation_id), state.model_dump(by_alias=True), ttl=TTL_SECONDS,
        ))
    except Exception:
        return False


def load_view_state(user_id: Any, conversation_id: int | None) -> dict | None:
    try:
        raw = biz_cache.get_json(_key(user_id, conversation_id))
    except Exception:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    return raw if isinstance(raw, dict) else None


def render_view_state_block(state: dict | None) -> str:
    """渲染成一段紧凑上下文。空状态返回空串（不占 prompt）。"""
    if not state:
        return ""
    parts: list[str] = []
    page = state.get("page")
    if page:
        parts.append({"kline": "K线页", "dashboard": "首页", "workbench": "工作台"}.get(page, str(page)))
    sym = state.get("symbol")
    if sym:
        mk = state.get("market") or ""
        parts.append(f"{mk + ':' if mk else ''}{sym}")
    if state.get("period"):
        parts.append(f"{state['period']} 周期")
    rng = state.get("range") or {}
    if isinstance(rng, dict) and (rng.get("from") or rng.get("to")):
        parts.append(f"视图区间 {rng.get('from', '?')}~{rng.get('to', '?')}")
    sel = state.get("selection") or {}
    if isinstance(sel, dict) and sel:
        a = sel.get("from") or sel.get("start")
        b = sel.get("to") or sel.get("end")
        if a or b:
            parts.append(f"用户圈选 {a or '?'}~{b or '?'}")
    if state.get("indicators"):
        parts.append("指标 " + "/".join(str(x) for x in state["indicators"][:6]))
    if state.get("anchor_line") is not None:
        parts.append(f"光标价位 {state['anchor_line']}")
    if not parts:
        return ""
    text = "【当前视图】" + " · ".join(parts)
    text += "\n（这是页面锚点，只用于补全用户没明说的标的/区间；用户文字里写了的标的以文字为准。）"
    return text[:300]


def append_view_state_block(messages: list[dict], user_id: Any, conversation_id: int | None) -> None:
    """把视图块拼到 system 消息末尾。任何异常静默降级(上下文是增强, 不是必需)。"""
    if not messages:
        return
    try:
        block = render_view_state_block(load_view_state(user_id, conversation_id))
    except Exception:
        return
    if not block:
        return
    first = messages[0]
    if first.get("role") == "system":
        first["content"] = (first.get("content") or "") + "\n\n" + block
