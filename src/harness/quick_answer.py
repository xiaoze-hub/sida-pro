"""问价快答：纯行情问题不调模型，直接把工具读数排版返回。

为什么要单开一条路（2026-10-22 实测）：问「600111 现价多少」走完整工具循环要 **70 秒**，
其中工具只占 2~5 秒，其余全是模型生成（生产模型单次 17~35s，一轮要 2 次调用）。
而这类问题根本不需要"判断"——工具已经给了确定数字，模型的唯一作用是把它念一遍。

这条路只处理**纯读数类**问题（现价/涨跌幅/成交量），一旦沾上分析意图立刻让回主流程：
「多少钱」走快答，「还能拿吗」必须走诊断（那是要证据链的）。
"""

from __future__ import annotations

import re

# 只要出现这些词就说明用户想知道"现在多少钱"
_QUOTE_WORDS = (
    "多少钱", "现价", "价格", "报价", "股价", "涨多少", "跌多少", "涨了", "跌了",
    "涨跌幅", "涨幅", "跌幅", "成交量", "成交额", "换手率", "多少手", "什么价",
)

# 出现任何一个就说明要判断，不许快答（判断必须有证据链，快答给不了）
_ANALYSIS_WORDS = (
    "怎么样", "怎么看", "分析", "诊断", "研判", "点评", "看法", "评价",
    "能买", "能拿", "持有", "该不该", "要不要", "值得", "风险", "机会",
    "为什么", "原因", "走势", "趋势", "支撑", "压力", "目标", "洗盘", "出货",
    "涨停", "跌停", "后面", "后续", "对比", "哪个好", "建议",
)

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")


def is_pure_quote(text: str) -> bool:
    """是不是「只问读数」的问题。"""
    t = (text or "").strip()
    if not t or len(t) > 40:          # 长句基本都带判断，别猜
        return False
    if any(w in t for w in _ANALYSIS_WORDS):
        return False
    return any(w in t for w in _QUOTE_WORDS)


def resolve_symbol(text: str, bound: str | None = None) -> str | None:
    """取标的：正文 6 位代码 > 会话绑定 > 正文里的股票名。"""
    m = _CODE_RE.search(text or "")
    if m:
        return m.group(1)
    if bound and str(bound).strip():
        return str(bound).split(".")[0].strip() or None
    try:
        from src.core.symbol_guard import first_code_from_names

        return first_code_from_names(text or "")
    except Exception:  # noqa: BLE001  解析失败就走主流程
        return None


async def answer(db, text: str, bound: str | None, user, runtime=None) -> str | None:
    """纯读数问题 → 直接调用行情工具并排版返回；任何不确定都返回 None（交回主流程）。

    `runtime` 只用来写轨迹：快答虽然没走模型，但确实调了工具，
    不记的话治理页会显示 tool_calls=0 —— 那正是"零工具作答"的特征，会被误读成幻觉。
    """
    if not is_pure_quote(text):
        return None
    symbol = resolve_symbol(text, bound)
    if not symbol:
        return None
    try:
        from src.agents.chat.registry import CHAT_TOOL_REGISTRY

        tool = CHAT_TOOL_REGISTRY.get("get_stock_quote")
        if tool is None:
            return None
        import time as _t

        _t0 = _t.perf_counter()
        raw = await tool.handler(db, {"symbol": symbol, "market": "CN"}, user)
        _ms = int((_t.perf_counter() - _t0) * 1000)
    except Exception:  # noqa: BLE001  旁路：失败就正常走模型
        return None
    if not raw or "未能获取" in raw:
        return None  # 没数据就别抢答，让主流程去降级/说明

    _trace = getattr(runtime, "trace", None)
    if _trace is not None:
        try:
            _trace.step("get_stock_quote", {"symbol": symbol, "market": "CN"},
                        ok=True, result=raw, elapsed_ms=_ms)
        except Exception:  # noqa: BLE001  埋点失败不影响作答
            pass

    return (
        f"【快答】{raw.strip()}\n\n"
        "（本条为行情读数直出，未经模型改写）\n"
        "以上为数据读数，不构成投资建议。"
    )
