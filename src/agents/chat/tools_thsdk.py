"""thsdk 完整能力注册到对话助手 — 11 个工具。

本模块把 thsdk 剩余 20 个待落地能力中的 11 个高价值工具封装成对话助手可调用的
工具函数(可降级、不抛异常到用户层),并通过 `THSDK_TOOL_REGISTRY` 注册给对话助手。

关键约束:
- **复用 thsdk_l2 已封装的能力**,不重复造轮子
- **全部通过 thsdk_l2 间接调用**,不直接 import thsdk
- 懒加载 thsdk_l2,失败/超时降级返回 ``available: false`` + 空 data + note
- 返回统一结构 ``{"available": bool, "data": list, "note": str}``

涉及的新增 thsdk_l2 能力(corporate_action / hs300 / bond / fund)已在
``data_source/thsdk_l2.py`` 补充对应封装方法,本模块只做间接转发。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# 工具统一返回结构 & 降级辅助
# ──────────────────────────────────────────────────────────────────────────


def _degraded(note: str) -> Dict[str, Any]:
    """返回统一的降级结构(available=False + 空 data)。"""
    return {"available": False, "data": [], "note": note}


def _df_to_records(df) -> List[Dict[str, Any]]:
    """把 thsdk_l2 返回的 DataFrame / dict / list 统一转成 JSON 安全的 records 列表。

    - DataFrame → records(dict 列表)
    - dict → [dict]
    - list → 原样返回(逐元素清洗为 dict)
    - 空/None → []
    """
    if df is None:
        return []
    records: List[Dict[str, Any]] = []
    if hasattr(df, "to_dict"):
        try:
            records = df.to_dict(orient="records")
        except Exception:  # 异常 DataFrame 结构
            records = []
    elif isinstance(df, dict):
        records = [df]
    elif isinstance(df, list):
        records = [r for r in df if isinstance(r, dict)]
    return records


def _safe_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Dict[str, Any]:
    """懒加载 thsdk_l2 并安全执行一次调用,失败降级。"""
    try:
        result = fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — 降级友好,不向用户层抛异常
        logger.warning("thsdk 工具调用失败 %s: %s", getattr(fn, "__name__", fn), e)
        return _degraded(f"thsdk 数据源不可用: {str(e)[:80]}")
    records = _df_to_records(result)
    if not records:
        return _degraded("thsdk 返回空数据(可能非交易时段、游客账户受限或数据源未就绪)。")
    return {"available": True, "data": records, "note": ""}


def _to_ths_code(symbol: str) -> str:
    """6 位纯代码 → thsdk 前缀代码(A 股)。

    规则(与 thsdk_l2 THS_PREFIX_* 一致):
      - 6 开头 → USHA(沪 A)
      - 0/3 开头 → USZA(深 A)
      - 4/8/9 开头 → USTM(北交所)
    非 6 位输入原样返回,交给 thsdk_l2 自行解析。
    """
    s = str(symbol or "").strip()
    if len(s) == 6 and s.isdigit():
        if s.startswith("6"):
            return f"USHA{s}"
        if s.startswith(("0", "3")):
            return f"USZA{s}"
        return f"USTM{s}"
    return s


def _l2():
    """懒加载 thsdk_l2 默认客户端(函数式 API,单例)。"""
    from data_source import thsdk_l2  # 局部导入,避免模块级循环依赖
    return thsdk_l2


# ──────────────────────────────────────────────────────────────────────────
# 11 个工具:个股资讯 / 公司行动 / DDE / 沪深300 / 扩展行情 / 指数 / 港美 /
# 可转债 / 基金 / 增强版问财
# ──────────────────────────────────────────────────────────────────────────


def get_thsdk_news(symbol: Optional[str] = None, limit: int = 5) -> Dict[str, Any]:
    """查询个股最新新闻(同花顺新闻源,实时)。

    args:
        symbol: 6 位股票代码,如 "002361";不填则返回市场级实时资讯。
        limit: 返回条数,默认 5。
    返回:
        {"available": bool, "data": [{title, time, content, source}, ...], "note": str}
    """
    limit = max(1, min(int(limit or 5), 50))
    try:
        sym = _to_ths_code(symbol) if symbol else None
        records = _df_to_records(_l2().get_news(sym))
    except Exception as e:  # noqa: BLE001
        return _degraded(f"thsdk 新闻源不可用: {str(e)[:80]}")
    if not records:
        return _degraded("thsdk 暂无实时新闻数据(可能非交易时段或数据源未就绪)。")

    normalized = []
    for r in records[:limit]:
        normalized.append(
            {
                "title": r.get("标题") or r.get("title") or r.get("名称") or "",
                "time": r.get("时间") or r.get("time") or r.get("日期") or "",
                "content": r.get("内容") or r.get("content") or r.get("摘要") or "",
                "source": r.get("来源") or r.get("source") or "",
            }
        )
    return {"available": True, "data": normalized, "note": ""}


def get_thsdk_corporate_action(symbol: str) -> Dict[str, Any]:
    """公司行动(分红/送股/拆股/回购等历史记录)。

    args:
        symbol: 6 位股票代码,如 "002361"。内部转为 thsdk 前缀代码。
    返回:
        {"available": bool, "data": [公司行动记录...], "note": str}
    """
    if not symbol:
        return _degraded("请提供股票代码(symbol)。")
    return _safe_call(_l2().get_corporate_action, _to_ths_code(symbol))


def get_thsdk_dde(symbol: str) -> Dict[str, Any]:
    """DDE 大单动向(同花顺官方主力资金, 比逐笔更精确的主力意图)。

    修复 2026-08-21(国内生产): thsdk 当前版本 THS 对象没有 `dde` 方法
    (线上报 'THS' object has no attribute 'dde'), 改走 `get_main_flow_official`
    (底层 query_data id=200 DDE 口径: 主力净流入 + 特大单/大单主动/被动明细),
    该接口在国内生产实测可用(2026-08-21 神剑 -5647万)。

    args:
        symbol: 6 位股票代码,如 "002361"。
    返回:
        {"available": bool, "data": [{symbol, price, main_net_amount_wan,
         main_net_ratio, summary, detail...}], "note": str}
    """
    if not symbol:
        return _degraded("请提供股票代码(symbol)。")
    result = _safe_call(_l2().get_main_flow_official, str(symbol).strip())
    # get_main_flow_official 失败时返回 {"symbol":..., "error": "no_code"} 而非抛异常,
    # _safe_call 会把它当成功 → 统一转降级结构
    if result.get("available") and isinstance(result.get("data"), list):
        for row in result["data"]:
            if isinstance(row, dict) and row.get("error"):
                return _degraded(f"thsdk DDE 查询失败: {row.get('error')}")
    return result


def get_thsdk_hs300_constituents() -> Dict[str, Any]:
    """沪深300 成分股列表(同花顺官方权威)。

    返回:
        {"available": bool, "data": [成分股记录...], "note": str}
    """
    return _safe_call(_l2().get_hs300_constituents)


def get_thsdk_market_data_cn_extended(symbol: str) -> Dict[str, Any]:
    """A 股扩展数据(含主力净流入、5 日资金流等游客账户拿不到的字段)。

    args:
        symbol: 6 位股票代码,如 "002361"。内部转为 thsdk 前缀代码。
    返回:
        {"available": bool, "data": [行情记录...], "note": str}
    """
    if not symbol:
        return _degraded("请提供股票代码(symbol)。")
    return _safe_call(_l2().get_market_data_cn_extended, _to_ths_code(symbol))


def get_thsdk_market_data_index(symbol: str = "000001") -> Dict[str, Any]:
    """指数实时行情(上证/深证/创业板/科创50 等)。

    args:
        symbol: 指数代码,如 "000001"(上证) / "399001"(深证) / "399006"(创业板)。
                仅数字时自动按沪深指数前缀解析。
    返回:
        {"available": bool, "data": [指数行情...], "note": str}
    """
    code = _resolve_index_code(symbol or "000001")
    return _safe_call(_l2().get_market_data_index, code)


def _resolve_index_code(symbol: str) -> str:
    """指数代码解析:6 位纯数字 → thsdk 指数前缀代码。"""
    s = str(symbol or "").strip()
    if len(s) == 6 and s.isdigit():
        if s.startswith("000") or s.startswith(("880", "899")):
            return f"USHI{s}"  # 上证指数
        return f"USZI{s}"  # 深证指数
    return s


def get_thsdk_market_data_hk(symbol: str) -> Dict[str, Any]:
    """港股实时行情(腾讯/东财无港股 L2,同花顺盘口齐全)。

    args:
        symbol: 港股代码,如 "00700"(腾讯)。内部转为 thsdk UHKG 前缀代码。
    返回:
        {"available": bool, "data": [港股行情...], "note": str}
    """
    if not symbol:
        return _degraded("请提供港股代码(symbol),如 00700。")
    code = str(symbol).strip()
    if code.isdigit():
        code = f"UHKG{code.zfill(5)}"
    return _safe_call(_l2().get_market_data_hk, code)


def get_thsdk_market_data_us(symbol: str) -> Dict[str, Any]:
    """美股实时行情。

    args:
        symbol: 美股代码,如 "AAPL"(苹果)。内部转为 thsdk UNQQ 前缀代码。
    返回:
        {"available": bool, "data": [美股行情...], "note": str}
    """
    if not symbol:
        return _degraded("请提供美股代码(symbol),如 AAPL。")
    code = str(symbol).strip()
    if not code.startswith(("UNQQ", "USXM")):
        code = f"UNQQ{code}"
    return _safe_call(_l2().get_market_data_us, code)


def get_thsdk_market_data_bond(symbol: Optional[str] = None) -> Dict[str, Any]:
    """可转债行情/排行。

    args:
        symbol: 可转债代码,如 "110059";不填则返回可转债排行/列表。
    返回:
        {"available": bool, "data": [可转债行情/排行...], "note": str}
    """
    if symbol:
        code = str(symbol).strip()
        if code.isdigit() and not code.startswith("USBK"):
            code = f"USBK{code}"
        return _safe_call(_l2().get_market_data_bond, code)
    return _safe_call(_l2().get_market_data_bond)


def get_thsdk_market_data_fund(symbol: Optional[str] = None) -> Dict[str, Any]:
    """基金/ETF 行情。

    args:
        symbol: 基金/ETF 代码,如 "510300";不填则返回基金列表/排行。
    返回:
        {"available": bool, "data": [基金/ETF 行情...], "note": str}
    """
    if symbol:
        code = str(symbol).strip()
        if code.isdigit() and not code.startswith("USZF"):
            code = f"USZF{code}"
        return _safe_call(_l2().get_market_data_fund, code)
    return _safe_call(_l2().get_market_data_fund)


def get_wencai_enhanced(query: str) -> Dict[str, Any]:
    """增强版问财 NLP(自然语言查询,如"均线多头,非ST,主力流入")。

    args:
        query: 自然语言选股条件,如 "均线多头排列,MACD金叉,非ST"。
    返回:
        {"available": bool, "data": [命中股票...], "note": str}
    """
    q = (query or "").strip()
    if not q:
        return _degraded("请提供自然语言查询条件(query),如 '均线多头排列,非ST,主力流入'。")
    return _safe_call(_l2().get_wencai_enhanced, q)


# ──────────────────────────────────────────────────────────────────────────
# 注册给对话助手的 registry
# ──────────────────────────────────────────────────────────────────────────

THSDK_TOOL_NAMES = [
    "get_thsdk_news",
    "get_thsdk_corporate_action",
    "get_thsdk_dde",
    "get_thsdk_hs300_constituents",
    "get_thsdk_market_data_cn_extended",
    "get_thsdk_market_data_index",
    "get_thsdk_market_data_hk",
    "get_thsdk_market_data_us",
    "get_thsdk_market_data_bond",
    "get_thsdk_market_data_fund",
    "get_wencai_enhanced",
]

# 中文名 + 描述(给 AI 看,便于决定何时调用)
THSDK_TOOL_META: Dict[str, Dict[str, str]] = {
    "get_thsdk_news": {
        "name_cn": "同花顺个股新闻",
        "desc": "查询个股最新新闻(同花顺新闻源,实时)。用户问「XX 股票/公司有什么新闻」「神剑新闻」等个股资讯类问题时调用。",
        "source": "thsdk(同花顺,游客/正式账户均可)",
    },
    "get_thsdk_corporate_action": {
        "name_cn": "公司行动",
        "desc": "公司行动(分红/送股/拆股/回购等历史记录)。用户问「XX 什么时候分红/送转股/回购」「公司行动历史」等时调用。",
        "source": "thsdk(同花顺,游客/正式账户均可)",
    },
    "get_thsdk_dde": {
        "name_cn": "DDE 大单动向",
        "desc": "DDE 大单动向(同花顺独有,比逐笔更精确的主力意图)。用户问「XX 大单动向/DDE」等时调用。",
        "source": "thsdk(同花顺,游客/正式账户均可)",
    },
    "get_thsdk_hs300_constituents": {
        "name_cn": "沪深300成分股",
        "desc": "沪深300 成分股列表(同花顺官方权威)。用户问「沪深300 成分股」「300成分有哪些」等时调用。",
        "source": "thsdk(同花顺,正式账户更稳)",
    },
    "get_thsdk_market_data_cn_extended": {
        "name_cn": "A股扩展行情",
        "desc": "A 股扩展行情(含主力净流入、5 日资金流等游客账户拿不到的字段)。用户问「XX 主力净流入/5日资金流」等详细资金面字段时调用。",
        "source": "thsdk(同花顺,主力净流入需正式账户,游客返0)",
    },
    "get_thsdk_market_data_index": {
        "name_cn": "指数实时行情",
        "desc": "指数实时行情(上证/深证/创业板/科创50 等)。用户问「大盘/上证指数/创业板指数现在多少点」等时调用。",
        "source": "thsdk(同花顺,游客/正式账户均可)",
    },
    "get_thsdk_market_data_hk": {
        "name_cn": "港股实时行情",
        "desc": "港股实时行情(腾讯/东财无港股 L2)。用户问「腾讯/港股 XX 现在股价」等时调用。",
        "source": "thsdk(同花顺,游客返0需正式账户)",
    },
    "get_thsdk_market_data_us": {
        "name_cn": "美股实时行情",
        "desc": "美股实时行情。用户问「苹果/AAPL/美股 XX 现在股价」等时调用。",
        "source": "thsdk(同花顺,游客/正式账户均可)",
    },
    "get_thsdk_market_data_bond": {
        "name_cn": "可转债行情",
        "desc": "可转债行情/排行。用户问「今天可转债异动/可转债行情/XX 转债现价」等时调用。",
        "source": "thsdk(同花顺,游客/正式账户均可)",
    },
    "get_thsdk_market_data_fund": {
        "name_cn": "基金/ETF行情",
        "desc": "基金/ETF 行情。用户问「XX ETF/基金净值/510300 行情」等时调用。",
        "source": "thsdk(同花顺,游客/正式账户均可)",
    },
    "get_wencai_enhanced": {
        "name_cn": "增强版问财",
        "desc": "增强版问财 NLP(自然语言选股)。用户问「帮我选 均线多头/主力流入/非ST 的股票」等自然语言选股问题时调用,比 tdx_wenda 更灵活。",
        "source": "thsdk(同花顺,游客/正式账户均可)",
    },
}

# 参数 schema(JSON Schema 风格),供 CHAT_TOOLS 注册
THSDK_TOOL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "get_thsdk_news": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "6 位股票代码,如 002361;不填返回市场级资讯"},
            "limit": {"type": "integer", "description": "返回条数,默认 5", "default": 5},
        },
        "required": [],
    },
    "get_thsdk_corporate_action": {
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "6 位股票代码,如 002361"}},
        "required": ["symbol"],
    },
    "get_thsdk_dde": {
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "6 位股票代码,如 002361"}},
        "required": ["symbol"],
    },
    "get_thsdk_hs300_constituents": {"type": "object", "properties": {}, "required": []},
    "get_thsdk_market_data_cn_extended": {
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "6 位股票代码,如 002361"}},
        "required": ["symbol"],
    },
    "get_thsdk_market_data_index": {
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "指数代码,如 000001/399001/399006", "default": "000001"}},
        "required": [],
    },
    "get_thsdk_market_data_hk": {
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "港股代码,如 00700"}},
        "required": ["symbol"],
    },
    "get_thsdk_market_data_us": {
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "美股代码,如 AAPL"}},
        "required": ["symbol"],
    },
    "get_thsdk_market_data_bond": {
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "可转债代码,如 110059;不填返回排行"}},
        "required": [],
    },
    "get_thsdk_market_data_fund": {
        "type": "object",
        "properties": {"symbol": {"type": "string", "description": "基金/ETF代码,如 510300;不填返回排行"}},
        "required": [],
    },
    "get_wencai_enhanced": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "自然语言选股条件,如 均线多头排列,非ST,主力流入"}},
        "required": ["query"],
    },
}

# 工具名 → 实现函数的映射(供 chat.py _execute_tool 转发)
THSDK_TOOL_HANDLERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "get_thsdk_news": get_thsdk_news,
    "get_thsdk_corporate_action": get_thsdk_corporate_action,
    "get_thsdk_dde": get_thsdk_dde,
    "get_thsdk_hs300_constituents": get_thsdk_hs300_constituents,
    "get_thsdk_market_data_cn_extended": get_thsdk_market_data_cn_extended,
    "get_thsdk_market_data_index": get_thsdk_market_data_index,
    "get_thsdk_market_data_hk": get_thsdk_market_data_hk,
    "get_thsdk_market_data_us": get_thsdk_market_data_us,
    "get_thsdk_market_data_bond": get_thsdk_market_data_bond,
    "get_thsdk_market_data_fund": get_thsdk_market_data_fund,
    "get_wencai_enhanced": get_wencai_enhanced,
}


def build_thsdk_tool_schemas() -> List[Dict[str, Any]]:
    """把 11 个 thsdk 工具构造成 CHAT_TOOLS 可用的 function schema 列表。"""
    schemas: List[Dict[str, Any]] = []
    for name in THSDK_TOOL_NAMES:
        meta = THSDK_TOOL_META[name]
        desc = f"{meta['name_cn']}。{meta['desc']} 数据源:{meta['source']}"
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": THSDK_TOOL_SCHEMAS[name],
                },
            }
        )
    return schemas


def format_thsdk_tool_result(name: str, result: Dict[str, Any]) -> str:
    """把工具统一返回 dict 渲染成给 AI/用户的可读文本。"""
    if not result.get("available"):
        return f"[{THSDK_TOOL_META[name]['name_cn']}] {result.get('note') or 'thsdk 数据源不可用。'}"
    records = result.get("data") or []
    lines = [f"[{THSDK_TOOL_META[name]['name_cn']} · thsdk 同花顺数据源] 共 {len(records)} 条:"]
    for i, r in enumerate(records[:50], 1):
        lines.append(f"{i}. {_compact_record(r)}")
    return "\n".join(lines)


def _compact_record(r: Dict[str, Any], max_len: int = 120) -> str:
    """把一条记录压成简短单行文本(优先取常见字段)。"""
    if not isinstance(r, dict):
        return str(r)[:max_len]
    order = ["名称", "代码", "title", "标题", "时间", "最新价", "涨跌幅", "内容", "summary", "price", "chg"]
    picked = []
    for k in order:
        if k in r and r[k] not in (None, ""):
            picked.append(f"{k}={r[k]}")
    if picked:
        return " ".join(picked)[:max_len]
    return str(r)[:max_len]


# 供外部(re-import chattools)引用的统一注册集
THSDK_TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    name: {
        "handler": THSDK_TOOL_HANDLERS[name],
        "schema": THSDK_TOOL_SCHEMAS[name],
        "meta": THSDK_TOOL_META[name],
    }
    for name in THSDK_TOOL_NAMES
}
