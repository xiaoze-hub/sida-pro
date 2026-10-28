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
    #: 标的类型（2026-10-28 P2）：`stock` / `index`。`000001` 既是上证指数也是平安银行，
    #: 标注提议/回读必须知道"用户看的是哪一类图"，否则只能整段禁用（旧做法）或猜（伪证）。
    #: 前端只报它真实渲染的那一个；缺 = 前端没报（不是"猜不到"，是不许猜）。
    instrument: str | None = None
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
        inst = state.get("instrument")
        label = {"stock": "个股", "index": "指数"}.get(str(inst or ""), "")
        parts.append(f"{label}{'/' if label else ''}{mk + ':' if mk else ''}{sym}")
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


def extract_selection_range(state: dict | None) -> tuple[str, str] | None:
    """从视图状态里取出圈选区间 (start_date, end_date)。

    前端可能推 `{from,to}` 也可能推 `{start,end}`（两版图表组件都用过），
    日期可能是 `2026-09-15` 或带时间的 ISO 串。任一缺失/认不出就返回 None ——
    圈选只补空，缺一半的区间不猜另一半。
    """
    if not isinstance(state, dict):
        return None
    sel = state.get("selection")
    if not isinstance(sel, dict):
        return None
    from src.core.trend_profile import normalize_day

    a = normalize_day(sel.get("from") or sel.get("start"))
    b = normalize_day(sel.get("to") or sel.get("end"))
    if not a or not b:
        return None
    if a > b:                        # 反向拖拽（从右往左圈）
        a, b = b, a
    return (a, b)


def append_view_state_block(
    messages: list[dict], user_id: Any, conversation_id: int | None
) -> dict | None:
    """把视图块拼到 system 消息末尾，并**返回**视图状态（2026-10-28 P2）。

    为什么要有返回值：标注提议需要"用户正在看哪张图"的 `instrument`（`000001` 是指数还是
    平安银行只能由页面说了算）。返回值让调用方能把它一路带到管线里，而不是从渲染出来的
    提示词文本里再解析一遍（那是散文当数据）。

    任何异常静默降级(上下文是增强, 不是必需)；拼不出内容时也返回 state（视图仍然有效）。
    """
    state: dict | None = None
    if not messages:
        return None
    try:
        state = load_view_state(user_id, conversation_id)
        block = render_view_state_block(state)
    except Exception:
        return None
    if not block:
        return state
    # 圈选区间实算注入: 用户圈了一段问"这段为什么跌", 涨跌/高低/回撤/量比必须自己算好,
    # 不能让模型反问"哪一段"或凭印象编。读库失败一律静默(增强项, 不能拖垮对话)。
    try:
        sym = str((state or {}).get("symbol") or "").strip()
        rng = extract_selection_range(state)
        if sym and rng:
            from src.core.trend_profile import build_range_summary

            summary = build_range_summary(
                sym, rng[0], rng[1], str((state or {}).get("market") or "CN"),
            )
            if summary:
                block = block + "\n" + summary
    except Exception:
        pass
    first = messages[0]
    if first.get("role") == "system":
        first["content"] = (first.get("content") or "") + "\n\n" + block
    return state
