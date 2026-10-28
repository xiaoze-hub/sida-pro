"""chat 工具注册表(W3.3/D3, 2026-09-09): schema 与 handler 同处注册。

照 src/agents/chat/tools_thsdk.py 的 THSDK_TOOL_HANDLERS 注册机制(方案 3.3 指定参考),
把原先散在 src/web/api/chat.py 的内联 CHAT_TOOLS schema(约 410 行)与 _execute_tool
if/elif 分支(约 580 行)收口为一个注册表: 每个工具一条记录, 含

- schema:    function schema(LLM 可见; None = handler-only, 不进 tools 清单)
- handler:   async (db, args, user) -> str
- caliber:   口径标签(数据源口径, 非空; get_capital_flow 为红线文案)
- requires:  权限点("" = 公共行情/市场数据, "self" = 只读本人数据, S5)

handler 内对 chat.py 上下文构建辅助函数(_build_portfolio_context 等)采用函数内
懒 import, 与 chat.py 反向不构成模块级循环依赖(chat.py → registry 单向)。
历史坑(2026-08-21): 工具分支的局部 `import asyncio` 曾使 asyncio 成为整个
_execute_tool 函数作用域的局部名, 引发 UnboundLocalError; 拆到本模块后每个
handler 独立作用域且模块顶层 import asyncio, 该类问题不复存在。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.db.models import User

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChatTool:
    name: str
    schema: dict | None
    handler: Callable[..., Any]
    caliber: str
    requires: str = ""
    # 是否计入本轮工具预算。取数工具=True；**只记录的工具=False**。
    # 为什么单列：记录类工具不取数、不产出证据，让它吃预算会把分析维度挤掉；
    # 更糟的是它常是最后一次调用，参数写错后预算已尽、模型无法重试（2026-10-22 生产实测：
    # 第 11 次调用传了 symbol 却没传 dimension/reason，被参数校验拦下 → 提案没入库）。
    charges_budget: bool = True
    # 是否允许进「无配方命中 → 全量工具」兜底路径。
    # False 的工具只在明确挂了它的配方里可见 —— 兜底路径同时服务闲聊/问价，
    # 把采集类工具放进去等于让模型在闲聊里自由写库（见 note_new_dimension 的注释）。
    fallback: bool = True


CHAT_TOOL_REGISTRY: dict[str, ChatTool] = {}


def register_chat_tool(
    name: str,
    *,
    schema: dict | None = None,
    caliber: str,
    requires: str = "",
    fallback: bool = True,
    charges_budget: bool = True,
):
    """注册对话工具(schema/handler/caliber/requires 同处一处), 重复注册报错。"""

    def _deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in CHAT_TOOL_REGISTRY:
            raise ValueError(f"chat 工具重复注册: {name}")
        CHAT_TOOL_REGISTRY[name] = ChatTool(
            name=name, schema=schema, handler=fn, caliber=caliber, requires=requires,
            fallback=fallback, charges_budget=charges_budget,
        )
        return fn

    return _deco


def chat_tool_schemas() -> list[dict]:
    """按注册顺序导出全部 function schema(handler-only 工具不导出)。"""
    return [t.schema for t in CHAT_TOOL_REGISTRY.values() if t.schema is not None]


def schema_tool_name(schema: dict) -> str:
    """从 function schema 取工具名（兼容两种写法，取不到返回空串）。"""
    if not isinstance(schema, dict):
        return ""
    fn = schema.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn.get("name"))
    return str(schema.get("name") or "")


def no_fallback_tool_names() -> frozenset[str]:
    """不允许进「无配方全量兜底」的工具名（registration 时 fallback=False 的那些）。

    routing 的两条兜底分支（stock_question / default 闲聊）据此剔除：
    采集类工具只在明确挂了它的配方里可见。
    """
    return frozenset(t.name for t in CHAT_TOOL_REGISTRY.values() if not t.fallback)


def charge_tool_budget(budget: dict | None, tool_name: str, label: str) -> str | None:
    """记一次工具预算；放行返回 None，超出则返回拒绝文案。

    **只记录类工具（charges_budget=False）不计费**：它们不产出证据，吃预算会挤掉取数维度；
    而且它们常是本轮最后一次调用，参数写错后必须留出余量重试
    （2026-10-22 生产实测：第 11 次调用参数不全被拒，预算已尽 → 提案没入库）。
    """
    if budget is None:
        return None
    tool = CHAT_TOOL_REGISTRY.get(tool_name)
    if tool is not None and not tool.charges_budget:
        return None
    if budget["used"] >= budget["max"]:
        return f"{label} 的工具预算（{budget['max']} 次）已用尽。请基于已取到的证据作答，缺失处标「暂无」。"
    budget["used"] += 1
    return None


def _filter_tool_args(handler: Callable, args: dict) -> dict:
    """按 handler 的可选参数过滤 args, 只传显式提供的参数, 避免 schema 与实现不一致。"""
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return dict(args or {})
    allowed = {
        p.name
        for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    }
    return {k: v for k, v in (args or {}).items() if k in allowed}


# ──────────────── 核心工具(原 chat.py 内联实现, 顺序 = 原 CHAT_TOOLS) ────────────────


def _perm_denied(user, db, perm: str) -> str | None:
    """聊天工具的权限收口(2026-09-18)。

    返回 None = 放行; 否则返回**直接给用户看的拒绝文案**。

    口径与 HTTP API / 外部 skill **完全同源**(`core.permissions.enforce_perm`, 含可调免费档的
    试用计数), 不另起一套 —— 避免"API 拦了聊天没拦"这类漏判。
    `user is None` 视为内部/系统调用(定时任务等无用户上下文), 不受商业档位限制; 网页聊天路径
    一定带 user, 所以不会成为绕过口子。
    """
    if user is None:
        return None
    try:
        from fastapi import HTTPException

        from src.core.permissions import enforce_perm

        enforce_perm(user, perm, db)
        return None
    except HTTPException as exc:
        detail = exc.detail
        msg = detail.get("message") if isinstance(detail, dict) else str(detail)
        return f"{msg}（升级 Pro 后可用；也可由管理员在「系统设置 · 免费档」里调整试用范围）"
    except Exception as exc:  # noqa: BLE001
        logger.warning("聊天工具权限校验异常, 保守拒绝 perm=%s: %r", perm, exc)
        return "权限校验失败，已保守拒绝本次调用。"


@register_chat_tool(
    "get_portfolio",
    schema={
        "type": "function",
        "function": {
            "name": "get_portfolio",
            "description": "获取用户的实盘持仓和模拟盘持仓。用于回答持仓相关问题（持仓健康吗、该调仓吗、盈亏情况等）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    caliber="库内数据: 实盘+模拟盘持仓(本人)",
    requires="self",
)
async def _tool_get_portfolio(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import _build_portfolio_context

    result = _build_portfolio_context(db, user=user)
    return result or "用户暂无持仓。"


@register_chat_tool(
    "get_stock_quote",
    schema={
        "type": "function",
        "function": {
            "name": "get_stock_quote",
            "description": "获取某只股票的实时行情（价格、涨跌幅、成交量等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 600519"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="多源实时行情聚合",
)
async def _tool_get_stock_quote(db: Session, args: dict, user: User | None = None) -> str:
    # 库优先(2026-10-22): 60s 内的库内快照直接用, 过期才走实时源 —— 与 stock.quote 域同一口径,
    # 避免「对话说 32.9 / 页面说 33.0」这种同数不同源。
    from src.web.api.chat import _fetch_realtime_context

    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    if market == "CN":
        row = _db_quote_snapshot(db, symbol)
        age = _quote_age_sec(row) if row else None
        if row is not None and row.get("last") is not None and age is not None and -5.0 <= age <= QUOTE_FRESH_SEC:
            name = row.get("name") or symbol
            return (
                f"行情（库内快照 {_hms(row.get('ts'))}）：{name}（CN:{symbol}）价格 {row.get('last')}，"
                f"涨跌幅 {row.get('change_pct')}%，成交量 {row.get('volume')}\n"
                f"{_caliber_db(row.get('ts'))}"
            )
    result = await _fetch_realtime_context(symbol, market)
    if result:
        return f"{result}\n口径：实时 RPC {_hms(_now_cn())}（marketdata）"
    return f"未能获取 {market}:{symbol} 的行情数据。\n{_caliber_missing('库无新鲜快照 / 实时源不可用')}"


@register_chat_tool(
    "get_technical_analysis",
    schema={
        "type": "function",
        "function": {
            "name": "get_technical_analysis",
            "description": "获取股票的技术面分析（趋势、MACD、RSI、支撑位、压力位等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="技术指标计算(基于K线数据)",
)
async def _tool_get_technical_analysis(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import _fetch_technical_context

    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    result = await _fetch_technical_context(symbol, market)
    return result or f"未能获取 {market}:{symbol} 的技术面数据。"


@register_chat_tool(
    "get_main_intent",
    schema={
        "type": "function",
        "function": {
            "name": "get_main_intent",
            "description": "获取股票当日主力意图（逐笔口径）：主力/超大单/大单净额、参与度、买占比、竞价、尾盘、筹码峰、成本带、获利盘。回答'主力在吸筹还是派发''主力意图如何'等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 002361"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="腾讯逐笔·主力意图口径",
)
async def _tool_get_main_intent(db: Session, args: dict, user: User | None = None) -> str:
    from src.agents.intraday_monitor import _main_intent_summary

    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    if market != "CN":
        return "主力意图仅支持 A 股(CN)。"
    try:
        # 热修 2026-08-14: 同步网络调用包 to_thread, 防阻塞 asyncio 事件循环(登录超时根因)
        result = await asyncio.to_thread(_main_intent_summary, symbol)
        # 数据源口径标注(腾讯逐笔·主力意图), 用户可见, 避免与东财四档混淆
        return f"[数据源: 腾讯逐笔·主力意图口径]\n{result}" if result else f"未能获取 {symbol} 的主力意图数据。"
    except Exception as e:
        return f"主力意图获取失败: {str(e)[:100]}"


@register_chat_tool(
    "get_decision_pioneer",
    schema={
        "type": "function",
        "function": {
            "name": "get_decision_pioneer",
            "description": "获取股票数智决策三指标（GS策略趋势 + 暗盘资金/L2主力净流入 + AI机构活跃度）：机构活跃度数值与档位(生命线1.56/强势线3/大牛线6)、连强天数、5日均值，GS策略G买/S卖信号与当前G区/S区状态，L2主力净流入(TQ口径,对齐同花顺暗盘)。回答'三指标共振''机构活跃度''GS策略信号''暗盘资金''数智决策'等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 002361"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="数智决策三指标·机构活跃度+GS+L2主力净流入(TQ口径)",
)
async def _tool_get_decision_pioneer(db: Session, args: dict, user: User | None = None) -> str:
    # 2026-09-18: 数智决策三指标(机构活跃度+GS+L2主力净流入 TQ口径)为 pro 专属 —— 聊天入口同样收口
    from src.core.permissions import PERM_VIEW_FORECAST

    denied = _perm_denied(user, db, PERM_VIEW_FORECAST)
    if denied:
        return denied
    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    if market != "CN":
        return "数智决策三指标仅支持 A 股(CN)。"
    try:
        from src.core.decision_pioneer import decision_pioneer_text

        # 热修: 同步网络调用包 to_thread, 防阻塞事件循环
        result = await asyncio.to_thread(decision_pioneer_text, symbol, market)
        return f"[数据源: 数智决策三指标·机构活跃度+GS+L2主力净流入]\n{result}" if result else f"未能获取 {symbol} 的数智决策数据。"
    except Exception as e:
        return f"数智决策获取失败: {str(e)[:100]}"


@register_chat_tool(
    "get_rally_analysis",
    schema={
        "type": "function",
        "function": {
            "name": "get_rally_analysis",
            "description": "分析股票当日盘中顺势拉升段（逐单明细）：识别所有放量拉升段，逐段拆解主力/散户买卖结构、主动买占比，判别'放量上涨(真拉升)'还是'拉高出货(假拉升)'。回答'拉升是拉高出货还是放量上涨''盘中拉升段分析'等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 002361"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="THS L2 逐笔·盘中拉升段口径",
)
async def _tool_get_rally_analysis(db: Session, args: dict, user: User | None = None) -> str:
    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    if market != "CN":
        return "拉升段分析仅支持 A 股(CN)。"
    try:
        from src.core.rally_analysis import analyze_rallies, format_rally_report

        # 热修 2026-08-14: 同步网络调用包 to_thread, 防阻塞事件循环
        result = await asyncio.to_thread(analyze_rallies, symbol)
        if not result:
            return f"未能获取 {symbol} 的拉升段分析数据(可能盘前无数据)。"
        return format_rally_report(result)
    except Exception as e:
        return f"拉升段分析失败: {str(e)[:100]}"


@register_chat_tool(
    "get_stock_suggestions",
    schema={
        "type": "function",
        "function": {
            "name": "get_stock_suggestions",
            "description": "获取某只股票最近的 AI 建议和分析报告。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="库内数据: AI 建议池(本人)",
    requires="self",
)
async def _tool_get_stock_suggestions(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import _build_stock_context

    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    result = _build_stock_context(db, symbol, market, user=user)
    return result or f"暂无 {market}:{symbol} 的 AI 建议。"


@register_chat_tool(
    "get_watchlist",
    schema={
        "type": "function",
        "function": {
            "name": "get_watchlist",
            "description": "获取用户的自选股（关注列表）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    caliber="库内数据: 自选股(本人+全局)",
    requires="self",
)
async def _tool_get_watchlist(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import _build_watchlist_context

    return _build_watchlist_context(db, user=user)


@register_chat_tool(
    "get_capital_flow",
    schema={
        "type": "function",
        "function": {
            "name": "get_capital_flow",
            "description": "获取某只 A 股的主力资金流向（主力净流入、超大单/大单/中单/小单净流入、5日主力净流入趋势）。用于回答资金面问题（主力在吸筹还是出货、资金流向如何等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 002361"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="东财四档，方向位与逐笔口径相反，禁止据此判定主力意图",
)
async def _tool_get_capital_flow(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import _fetch_capital_flow_context

    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    result = await _fetch_capital_flow_context(symbol, market)
    # 数据源口径标注(东财四档·资金流向), 与 get_main_intent(逐笔) 区分
    return f"[数据源: 东财四档·资金流向口径]\n{result}" if result else f"未能获取 {market}:{symbol} 的资金流向数据。"


def _wenda_index_fallback(question: str) -> str:
    """问小达没数据时，若问的是**指数点位**就用腾讯实时兜底（2026-10-25）。

    生产事故：用户问「上证指数现在多少点」，模型这次选了 `tdx_wenda`（而非
    `get_thsdk_market_data_index`），问小达返回空 → 直接答「查不到上证点位」。
    同一时刻首页指数条是好的 —— **不是没数据，是选错入口且这条入口没兜底**。

    只在问题里出现指数名/代码时才兜底：市场级排行问题（今日主力净流入前10）
    不该被指数行情顶掉，宁可如实说"问小达无结果"。
    """
    try:
        from src.agents.chat.tools_thsdk import _tencent_index_fallback
        from src.core.index_codes import INDEX_CODES

        q = question or ""
        code = ""
        # 先按代码精确命中（000001 / 399001 …）
        for c in INDEX_CODES:
            if c in q:
                code = c
                break
        if not code:
            # 再按名称：全称优先，其次短名（"上证"→上证指数、"创业板"→创业板指）
            for c, name in INDEX_CODES.items():
                if name and name in q:
                    code = c
                    break
            if not code:
                for c, name in INDEX_CODES.items():
                    short = name.replace("指数", "")
                    if short and short in q:
                        code = c
                        break
        if not code:
            return ""
        rows = _tencent_index_fallback(code)
        if not rows:
            return ""
        r0 = rows[0] if isinstance(rows[0], dict) else {}
        price = r0.get("price")
        pct = r0.get("change_pct")
        name = r0.get("name") or INDEX_CODES.get(code, "")
        parts = [f"{name} 现价 {price}"]
        if pct is not None:
            try:
                parts.append(f"{float(pct):+.2f}%")
            except Exception:
                pass
        return (
            "[指数实时行情 · 问小达无结果，已用腾讯实时兜底（与首页指数条同源）]\n"
            + "  ".join(parts)
        )
    except Exception:
        return ""


@register_chat_tool(
    "tdx_wenda",
    schema={
        "type": "function",
        "function": {
            "name": "tdx_wenda",
            "description": "通达信问小达：自然语言投研查询。**只用于市场级排行/选股/资金流向**，如“今日主力净流入前10的A股”“今日涨幅前10的概念板块”“今日涨停家数最多的概念板块”“今日龙虎榜机构净买入前10”。取**指数点位**（上证/深证/创业板/沪深300 现在多少点）不要用它，用 get_thsdk_market_data_index（那边有腾讯兜底）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "自然语言查询，如 “今日主力净流入前10的A股”"},
                },
                "required": ["question"],
            },
        },
    },
    caliber="通达信问小达(市场级排行/选股口径)",
)
async def _tool_tdx_wenda(db: Session, args: dict, user: User | None = None) -> str:
    question = (args.get("question") or "").strip()
    if not question:
        return "请提供查询问题, 如 '今日主力净流入前10的A股'。"
    try:
        from marketdata.vendors.tdx import ask_wenda

        # 热修 2026-08-14: 同步网络调用包 to_thread, 防阻塞事件循环
        res = await asyncio.to_thread(ask_wenda, question)
        if not res or not isinstance(res, dict):
            fb = _wenda_index_fallback(question)
            return fb or f"通达信问小达未返回数据: {question}"
        rows = res.get("data") or []
        if not rows:
            fb = _wenda_index_fallback(question)
            return fb or f"通达信问小达无结果: {question}"
        lines = [f"通达信问小达查询结果「{question}」(共{len(rows)}条):"]
        for r in rows[:15]:
            if not isinstance(r, dict):
                continue
            name = r.get("sec_name") or r.get("name") or ""
            code = r.get("sec_code") or r.get("code") or ""
            chg = r.get("chg") or r.get("change_pct") or ""
            main_net = next(
                (v for k, v in r.items() if "主力净额" in k or "主力净" in k),
                "",
            )
            line = f"- {code} {name}"
            if chg:
                line += f" 涨{chg}%"
            if main_net:
                line += f" 主力净额{main_net}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"TDX 问小达工具失败: {e}")
        return f"通达信问小达查询失败: {e}"


@register_chat_tool(
    "get_market_news",
    schema={
        "type": "function",
        "function": {
            "name": "get_market_news",
            "description": "获取市场资讯与新闻：聚合全网/财经媒体热点话题(news_hotlist)与 AI 生成的每日市场简报(briefings, 早/午/收盘/晚盘)。用于回答「有什么新闻/资讯/热点」「今天消息面」「近期题材催化」等新闻资讯类问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "资讯热榜返回条数，默认 15", "default": 15},
                    "briefing_type": {"type": "string", "description": "每日简报类型：morning(早盘)/noon(午盘)/close(收盘)/evening(晚间)，不填则返回全部", "default": ""},
                },
                "required": [],
            },
        },
    },
    caliber="多源快讯聚合(财联社/新浪/东财7x24)+悟道热榜/简报",
)
async def _tool_get_market_news(db: Session, args: dict, user: User | None = None) -> str:
    limit = int((args.get("limit") or 15))
    briefing_type = (args.get("briefing_type") or "").strip()
    try:
        # 1) 底层多源快讯体系(财联社/新浪/东财7x24, 市场级, 引擎主备+降级)
        parts = []
        try:
            from src.core.marketdata_client import get_market_data

            items = await asyncio.to_thread(
                lambda: get_market_data().flash_news(market="CN", limit=limit)
            )
            if items:
                # 2026-10-24: 顺手落库 —— news_cache 此前 0 行(只拉不存), 导致参谋
                # 无法回答"上周政策时间线"这类跨时间问题。沉淀是旁路, 失败不影响取数。
                try:
                    from src.core.news_store import save_flash_news

                    added = save_flash_news(db, items)
                    if added:
                        logger.debug("news_cache 新增 %s 条快讯", added)
                except Exception as _e:  # noqa: BLE001
                    logger.debug("快讯沉淀失败(忽略): %s", _e)
                lines = [f"【市场快讯】(最近{len(items)}条, 多源聚合)"]
                for it in items[:limit]:
                    title = (getattr(it, "title", "") or "").strip()
                    src = (getattr(it, "source", "") or "").strip()
                    ts = getattr(it, "publish_time", None)
                    tstr = ts.strftime("%H:%M") if ts else ""
                    line = f"- [{tstr}] {title}"
                    if src:
                        line += f" ({src})"
                    lines.append(line)
                parts.append("\n".join(lines))
        except Exception as e:
            logger.warning(f"快讯聚合失败, 回退悟道热榜: {e}")

        # 2) 悟道热榜/简报作为补充(失败不影响主链路)
        try:
            from src.collectors.wudao_mcp_client import WudaoMCPClient

            cli = WudaoMCPClient()
            # 热修 2026-08-14: wudao MCP 同步 requests(timeout 30-60s)包 to_thread, 防阻塞事件循环
            await asyncio.to_thread(cli._initialize)
            hot = await asyncio.to_thread(cli.call_tool, "news_hotlist", {"limit": limit})
            hot_text = hot.get("text") if isinstance(hot, dict) else ""
            if hot_text:
                parts.append("【资讯热榜】\n" + str(hot_text))
            else:
                hot_rows = hot.get("rows") or hot.get("data") or hot.get("items") or []
                if isinstance(hot_rows, dict):
                    hot_rows = hot_rows.get("rows") or hot_rows.get("data") or []
                if hot_rows:
                    lines = ["【资讯热榜】"]
                    for r in hot_rows[:limit]:
                        if isinstance(r, dict):
                            title = r.get("title") or r.get("name") or r.get("keyword") or ""
                            heat = r.get("heat") or r.get("hot") or r.get("count") or ""
                            tag = r.get("tag") or r.get("source") or ""
                            line = "- " + str(title)
                            if tag:
                                line += " #" + str(tag)
                            if heat:
                                line += " (热度" + str(heat) + ")"
                            lines.append(line)
                    parts.append("\n".join(lines))
            brief_args = {"detailLevel": "digest"}
            if briefing_type:
                brief_args["type"] = briefing_type
            brief = await asyncio.to_thread(cli.call_tool, "briefings", brief_args)
            brief_text = ""
            if isinstance(brief, dict):
                brief_text = brief.get("text") or brief.get("digest") or ""
            if brief_text:
                parts.append("【每日简报】\n" + str(brief_text))
        except Exception as e:
            logger.debug(f"悟道热榜/简报失败(不影响主链路): {e}")

        if not parts:
            return "暂无实时资讯数据（可能非交易时段或数据源未就绪），建议盘后重试。"


        return "\n\n".join(parts)
    except Exception as e:
        logger.error(f"get_market_news 工具失败: {e}")
        return f"资讯获取失败: {e}"


@register_chat_tool(
    "get_kline_patterns",
    schema={
        "type": "function",
        "function": {
            "name": "get_kline_patterns",
            "description": "识别某只股票的K线组合形态（金针探底/双针探底/红三兵/涨停双响炮/揭竿而起/上升三法/小步上扬/放量突破/三只乌鸦/空方炮/黄昏之星等）。基于同花顺K线形态教学体系。用于回答「XX股票K线什么形态」「有没有金针探底/红三兵」「技术形态怎么样」等问题。返回形态名称+信号方向+特征描述。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 600519"},
                    "market": {"type": "string", "description": "市场：CN(默认)/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="同花顺K线形态教学体系(基于日K)",
)
async def _tool_get_kline_patterns(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import _fetch_kline_pattern_context

    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    return await _fetch_kline_pattern_context(symbol, market)


@register_chat_tool(
    "get_auction_data",
    schema={
        "type": "function",
        "function": {
            "name": "get_auction_data",
            "description": "获取A股集合竞价数据（9:25后当日竞价已生成）。场景：overview(竞价全景)、strongest(全市场最强个股)、theme(主线题材)、weak_to_strong(弱转强)、risk(被核风险)、watchlist(我的持仓+自选逐票竞价)、symbols(指定代码逐票竞价)。⚠️ 问「我的持仓/自选股竞价怎么样」「600111 竞价如何」这类**指定标的**的问题，必须用 watchlist 或 symbols 场景（逐票给开盘涨幅/竞价成交额/一字买量）；不要用 strongest 再下「不在榜单所以没强度」的结论——不在榜单≠没有竞价数据。9:25 前当日竞价数据未生成，会明确提示。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "description": "overview(默认)/strongest/theme/weak_to_strong/risk/watchlist(我的自选逐票)/symbols(指定代码逐票)"},
                    "symbols": {"type": "array", "items": {"type": "string"}, "description": "逐票查询的股票代码，如 [\"600111\",\"002361\"]；与 scene=symbols 配合"},
                    "limit": {"type": "integer", "description": "返回条数，默认 10", "default": 10},
                },
                "required": [],
            },
        },
    },
    caliber="集合竞价池(9:25 竞价数据)",
)
async def _tool_get_auction_data(db: Session, args: dict, user: User | None = None) -> str:
    # 2026-09-18: 集合竞价池(9:25 竞价数据)为 pro 专属 —— 聊天入口同样收口
    from src.core.permissions import PERM_VIEW_AUCTION

    denied = _perm_denied(user, db, PERM_VIEW_AUCTION)
    if denied:
        return denied
    from src.web.api.chat import _fetch_auction_context

    scene = args.get("scene", "overview")
    limit = int(args.get("limit", 10) or 10)
    symbols = [str(x).strip() for x in (args.get("symbols") or []) if str(x).strip()]
    return await _fetch_auction_context(scene, limit, symbols=symbols, db=db, user=user)


@register_chat_tool(
    "get_forecast",
    schema={
        "type": "function",
        "function": {
            "name": "get_forecast",
            "description": "读取系统 AI 预测引擎的最近预测结果（预测方向/预期涨跌幅/目标价/到期时间，数据来自预测引擎独立库）。⚠️ 历史回测准确率仅31.7%，预测方向不可靠，仅供参考，不可作为交易依据。用于回答「系统预测了什么」「预测引擎今天给了什么预测」「XX股票的预测结果怎么样」「预测目标价是多少」等问题。可选按股票代码过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 002361；不填则返回全部最近预测", "default": ""},
                    "limit": {"type": "integer", "description": "返回条数，默认 5", "default": 5},
                },
                "required": [],
            },
        },
    },
    caliber="预测引擎独立库(历史回测准确率仅31.7%, 不可作交易依据)",
)
async def _tool_get_forecast(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import _read_forecast

    symbol = (args.get("symbol") or "").strip()
    limit = int(args.get("limit", 5) or 5)
    return _read_forecast(symbol, limit)


# handler-only 工具: LLM 不可见(无 schema), 仅保留兼容入口(原 _execute_tool 同款分支)
@register_chat_tool(
    "get_opportunities",
    schema=None,
    caliber="库内数据: 今日机会候选(EntryCandidate)",
)
async def _tool_get_opportunities(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import _read_opportunities

    limit = int(args.get("limit", 10) or 10)
    return _read_opportunities(db, limit)


@register_chat_tool(
    "get_sentiment_cycle",
    schema={
        "type": "function",
        "function": {
            "name": "get_sentiment_cycle",
            "description": "读取当前 A 股短线情绪周期(冰点/修复/发酵/高潮/退潮)及操作提示。用于回答「现在市场情绪怎么样」「短线情绪处于什么阶段」「现在适合打板还是防守」等问题。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    caliber="涨停池/连板高度情绪周期模型",
)
async def _tool_get_sentiment_cycle(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import _read_sentiment_cycle

    base = await _read_sentiment_cycle()
    # 2026-09-25: 追加自研市场广度指标（涨跌家数/ADL/ADR/ARMS/BTI/MCL/STIX + 情绪温度）。
    # 只影响本 chat 工具出口，不改 _read_sentiment_cycle（前端与其他调用方不受影响）。
    try:
        from src.collectors.market_breadth_series import latest_with_percentile, render_text

        extra = render_text(latest_with_percentile(db))
    except Exception as exc:  # noqa: BLE001
        logger.warning("breadth section failed: %s", exc)
        extra = ""
    return (base + "\n\n" + extra) if extra else base


@register_chat_tool(
    "get_strategy_signals",
    schema={
        "type": "function",
        "function": {
            "name": "get_strategy_signals",
            "description": "读取系统最新策略信号（哪个策略对哪只股票给出了买/关注类信号、动作、得分与信号描述，取最新交易日）。用于回答「哪个策略给了信号」「今天策略信号有哪些」「XX策略有信号吗」「系统策略看好什么」等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回条数，默认 10", "default": 10},
                },
                "required": [],
            },
        },
    },
    caliber="库内数据: 策略信号(最新交易日)",
)
async def _tool_get_strategy_signals(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import _read_strategy_signals

    limit = int(args.get("limit", 10) or 10)
    return _read_strategy_signals(db, limit)


@register_chat_tool(
    "get_notifications",
    schema={
        "type": "function",
        "function": {
            "name": "get_notifications",
            "description": "读取系统最近通知/提醒（Agent 运行、报告生成、策略刷新等完成或失败消息）。用于回答「有什么通知」「系统有什么提醒」「有没有未读通知」等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回条数，默认 10", "default": 10},
                    "unread_only": {"type": "boolean", "description": "是否只返回未读通知，默认 False", "default": False},
                },
                "required": [],
            },
        },
    },
    caliber="库内数据: 系统通知(本人)",
    requires="self",
)
async def _tool_get_notifications(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import _read_notifications

    limit = int(args.get("limit", 10) or 10)
    unread_only = bool(args.get("unread_only") or False)
    return _read_notifications(db, limit, unread_only, user=user)


@register_chat_tool(
    "get_fundamentals_detail",
    schema={
        "type": "function",
        "function": {
            "name": "get_fundamentals_detail",
            "description": "获取个股基本面明细（合并五类）：龙虎榜（近10日上榜记录，含净买入/买卖额）、融资融券（两融余额/融资买入偿还）、股东户数（最新一期及环比变化）、分红历史（每股派息/送转）、事件日历（近7日公告/业绩预告）。用于回答「XX最近有什么龙虎榜/分红/股东户数变化/融资融券/事件公告」等个股基本面明细类问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 002361"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="龙虎榜/两融/股东户数/分红/事件(公开数据聚合)",
)
async def _tool_get_fundamentals_detail(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import _format_fundamentals_text

    symbol = (args.get("symbol") or "").strip()
    market = args.get("market", "CN")
    if not symbol:
        return "请提供股票代码(symbol)。"
    try:
        # 懒加载避免模块级循环依赖; 同步取数放线程池不阻塞事件循环
        from src.web.api.market_data import fetch_fundamentals_detail

        data = await asyncio.to_thread(fetch_fundamentals_detail, symbol, market)
        return _format_fundamentals_text(symbol, market, data)
    except Exception as e:
        logger.warning(f"get_fundamentals_detail 工具失败 [{symbol}]: {e}")
        return f"基本面明细查询失败: {e}"


@register_chat_tool(
    "get_irm_qa",
    schema={
        "type": "function",
        "function": {
            "name": "get_irm_qa",
            "description": "获取个股互动易问答（巨潮 cninfo）：投资者提问与公司官方回应列表（问题+回复+时间）。互动易是公司对投资者提问的官方回复，是验证传闻/利好的权威信源。用于回答「XX公司最近有什么互动易回复」「公司对XX事的官方回应」「管理层怎么回应XX传闻」等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 002361"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="巨潮 cninfo 互动易(公司官方回应)",
)
async def _tool_get_irm_qa(db: Session, args: dict, user: User | None = None) -> str:
    # 互动易问答(巨潮 cninfo): 公司对投资者提问的官方回应, 验证传闻/利好的权威信源
    symbol = (args.get("symbol") or "").strip()
    market = args.get("market", "CN")
    if not symbol:
        return "请提供股票代码(symbol)。"
    if market != "CN":
        return "互动易问答仅支持 A 股(CN)。"
    try:
        from marketdata.symbol import Symbol
        from marketdata.vendors.cninfo_irm import CninfoIrmVendor

        vendor = CninfoIrmVendor()
        # 同步 vendor 放线程池执行, 避免阻塞事件循环
        items = await asyncio.to_thread(
            vendor.fetch, [Symbol.parse(symbol)], {"page_size": 10}
        )
        if not items:
            return f"{symbol} 暂无互动易问答记录(可能近期无提问或接口暂未收录)。"
        lines = [f"【互动易问答】{symbol}(数据源: 巨潮 cninfo, 最近{len(items)}条):"]
        for i, it in enumerate(items[:10], 1):
            q = (getattr(it, "title", "") or "").strip()
            answer = (getattr(it, "url", "") or "").strip()  # 公司回复临时存 url 字段
            ts = getattr(it, "publish_time", None)
            tstr = ts.strftime("%Y-%m-%d") if ts else ""
            lines.append(f"{i}. [{tstr}] {q}")
            if answer:
                lines.append(f"   回复: {answer[:200]}")
            else:
                lines.append("   回复: (公司尚未回复)")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_irm_qa 工具失败 [{symbol}]: {e}")
        return f"互动易问答查询失败: {e}"


@register_chat_tool(
    "get_market_anomalies",
    schema={
        "type": "function",
        "function": {
            "name": "get_market_anomalies",
            "description": "获取A股异动股池（东财，交易所「严重异常波动」口径）：触发严重异常波动规则的个股列表，含代码/名称/当日涨跌幅/累计偏离/统计窗口/规则说明/是否当日。用于回答「今天有什么异动股」「哪些股票严重异常波动」「被交易所点名波动的股票」等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "返回条数，默认 20", "default": 20},
                },
                "required": [],
            },
        },
    },
    caliber="东财·交易所「严重异常波动」口径",
)
async def _tool_get_market_anomalies(db: Session, args: dict, user: User | None = None) -> str:
    # 东财异动池: 交易所「严重异常波动」口径, 市场级(无视个股参数)
    limit = min(int(args.get("limit", 20) or 20), 50)
    try:
        from marketdata.vendors.em_anomaly import EmAnomalyVendor

        vendor = EmAnomalyVendor()
        items = await asyncio.to_thread(
            vendor.fetch, [], {"page_size": limit}
        )
        if not items:
            return "东财异动池暂无数据(今日可能无触发「严重异常波动」规则的个股, 或非交易时段/数据源未就绪)。"
        has_today = any(getattr(it, "is_today", False) for it in items)
        lines = [
            f"【东财异动池】交易所「严重异常波动」标的(共{len(items)}条, "
            f"{'含当日' if has_today else '最近交易日'}):"
        ]
        for i, it in enumerate(items[:limit], 1):
            chg = getattr(it, "change_pct", None)
            dev = getattr(it, "deviation", None)
            days = getattr(it, "days", None)
            rule = getattr(it, "rule", "") or ""
            chg_s = f" 涨跌幅{chg:+.2f}%" if isinstance(chg, (int, float)) else ""
            dev_s = f" 累计偏离{dev:+.2f}%" if isinstance(dev, (int, float)) else ""
            days_s = f"({days}日)" if days else ""
            flag = "当日" if getattr(it, "is_today", False) else "非当日"
            lines.append(
                f"{i}. {it.symbol} {getattr(it, 'name', '') or ''} "
                f"{chg_s}{dev_s}{days_s} [{flag}]"
            )
            if rule:
                lines.append(f"   规则: {rule}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_market_anomalies 工具失败: {e}")
        return f"异动股池查询失败: {e}"


@register_chat_tool(
    "get_northbound",
    schema={
        "type": "function",
        "function": {
            "name": "get_northbound",
            "description": "获取北向资金(沪股通)当日净额：同花顺口径, 返回当日沪股通净买入(亿元)。用于回答「今天北向资金流入还是流出」「北向净买多少」「外资动向」等问题。注意: 2024-08 后交易所停止披露北向实时净买入, 此为同花顺估算口径仅供参考; 深股通数据暂缺。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    caliber="同花顺估算口径(2024-08 后交易所停止披露实时净买入, 仅供参考)",
)
async def _tool_get_northbound(db: Session, args: dict, user: User | None = None) -> str:
    # 北向资金(同花顺口径): 当日沪股通净额(亿元)。
    # 2024-08 后交易所停止披露实时净买入, 同花顺估算口径仅供参考; 深股通暂缺。
    try:
        from src.core.data_collector import DataCollectorManager

        mgr = DataCollectorManager()

        def _fetch_nb():
            from src.db.session import SessionLocal
            from src.db.models import DataSource

            db = SessionLocal()
            try:
                src = db.query(DataSource).filter(DataSource.type == "northbound").first()
                if not src:
                    return None, "未注册北向资金数据源"
                return asyncio.run(mgr._test_northbound_source(src)), None
            finally:
                db.close()

        r, err = await asyncio.to_thread(_fetch_nb)
        if err or r is None:
            return f"北向资金查询失败: {err or '无结果'}"
        if not r.success:
            return f"北向资金查询失败: {(r.error or '数据源异常')[:100]}"
        data = r.data or []
        if not data:
            return "北向资金暂无数据(非交易时段或数据源未就绪)。"
        lines = ["【北向资金】(同花顺估算口径, 仅供参考):"]
        for row in data:
            date = row.get("date", "")
            hgt = row.get("hgt_net")
            total = row.get("total_net")
            hgt_s = (
                f"沪股通净{'流入' if (hgt or 0) >= 0 else '流出'} {abs(hgt):.2f}亿"
                if isinstance(hgt, (int, float))
                else "沪股通暂缺"
            )
            total_s = (
                f" | 北向合计净额 {total:.2f}亿"
                if isinstance(total, (int, float))
                else " | 合计口径暂缺(深股通未披露)"
            )
            lines.append(f"- {date}: {hgt_s}{total_s}")
        lines.append(
            "注: 2024-08 起交易所停止披露北向实时净买入, 以上为同花顺估算口径; "
            "主力意图判断请以 get_main_intent 为准。"
        )
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_northbound 工具失败: {e}")
        return f"北向资金查询失败: {e}"


@register_chat_tool(
    "get_hot_stocks",
    schema={
        "type": "function",
        "function": {
            "name": "get_hot_stocks",
            "description": "获取同花顺热榜：按人气排名的热门A股（小时榜/日榜），含排名/代码/名称/涨跌幅/热度/概念标签/AI归因。用于回答「今天什么股票最热」「热榜前几」「XX为什么涨/为什么这么火」等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {"type": "string", "description": "热榜周期：hour(小时榜，默认)/day(日榜)", "default": "hour"},
                    "limit": {"type": "integer", "description": "返回条数，默认 20", "default": 20},
                },
                "required": [],
            },
        },
    },
    caliber="同花顺热榜(人气排名口径)",
)
async def _tool_get_hot_stocks(db: Session, args: dict, user: User | None = None) -> str:
    # 同花顺热榜(小时榜/日榜): 排名/热度/概念标签/AI归因(analyse)
    period = (args.get("period") or "hour").strip().lower()
    if period not in ("hour", "day"):
        period = "hour"
    limit = min(int(args.get("limit", 20) or 20), 50)
    try:
        from marketdata.vendors.ths_hot import ThsHotListVendor

        vendor = ThsHotListVendor()
        items = await asyncio.to_thread(
            vendor.fetch, [], {"period": period, "limit": limit}
        )
        if not items:
            return "同花顺热榜暂无数据(可能非交易时段或数据源未就绪)。"
        period_cn = "小时榜" if period == "hour" else "日榜"
        lines = [
            f"【同花顺热榜·{period_cn}】人气前{len(items)}(数据源: 同花顺, 含AI归因):"
        ]
        for i, it in enumerate(items[:limit], 1):
            rank = getattr(it, "rank", 0) or 0
            chg = getattr(it, "change_pct", None)
            heat = getattr(it, "heat", None)
            concepts = getattr(it, "concepts", ()) or ()
            reason = (getattr(it, "reason", "") or "").strip()
            chg_s = f" 涨跌幅{chg:+.2f}%" if isinstance(chg, (int, float)) else ""
            heat_s = f" 热度{heat}" if heat not in (None, "") else ""
            tag_s = (" 概念: " + "/".join(str(c) for c in concepts)) if concepts else ""
            lines.append(
                f"{i}. 第{rank}名 {it.symbol} {getattr(it, 'name', '') or ''}"
                f"{chg_s}{heat_s}{tag_s}"
            )
            if reason:
                lines.append(f"   AI归因: {reason[:150]}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_hot_stocks 工具失败: {e}")
        return f"同花顺热榜查询失败: {e}"


@register_chat_tool(
    "get_web_content",
    schema={
        "type": "function",
        "function": {
            "name": "get_web_content",
            "description": "抓取网页链接正文(支持 http/https, 含微信公众号文章 mp.weixin.qq.com)。用于回答「帮我看看这个链接/这篇文章讲了什么/分析一下这个网页内容」等需要分析用户发来链接的问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要抓取的网页完整链接, 如 https://mp.weixin.qq.com/s/xxx 或 https://example.com/article"},
                },
                "required": ["url"],
            },
        },
    },
    caliber="网页正文抓取(无金融口径)",
)
async def _tool_get_web_content(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api.chat import get_web_content as _fetch_page

    url = (args.get("url") or "").strip()
    if not url:
        return "请提供要抓取的网页链接(url)。"
    # 同步网络抓取放线程池, 不阻塞事件循环(与 2026-08-14 热修风格一致)
    return await asyncio.to_thread(_fetch_page, url)


@register_chat_tool(
    "get_main_flow_compare",
    schema={
        "type": "function",
        "function": {
            "name": "get_main_flow_compare",
            "description": "比对两路主力资金数据源(腾讯逐笔/同花顺L2)的一致性, 判断主力真实意图。仅限A股(CN)。入参 symbol=6位A股代码如002361。返回每路主力净额(元)及一致性评分(0-100)。用于回答「两路主力数据是否一致」「主力在吸筹还是派发」「各数据源口径对比」等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6位A股代码, 如 002361"},
                    "market": {"type": "string", "description": "市场代码, 仅支持 CN", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="腾讯逐笔/同花顺L2 双源对比(一致性评分口径)",
)
async def _tool_get_main_flow_compare(db: Session, args: dict, user: User | None = None) -> str:
    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    if market != "CN":
        return "主力双源对比仅支持 A 股(CN)。"
    try:
        from src.core.main_flow_compare import compare_main_flow

        result = await asyncio.to_thread(compare_main_flow, symbol)
        if not result or result.get("consistency") is None:
            return f"[数据源: 腾讯逐笔/同花顺L2] {symbol} 主力双源数据均不可用, 无法比对。"
        lines = [f"[数据源: 腾讯逐笔/同花顺L2] {symbol} 主力双源对比:"]
        lines.append(f"  一致性: {result['consistency']}/100  |  发散幅度: {result['delta_pct']}%")
        for src_name, src_key in [("腾讯逐笔", "tencent"), ("同花顺L2", "thsdk")]:
            src = result.get(src_key)
            if src and src.get("available"):
                mn = src.get("main_net")
                mn_str = f"{mn:+,.0f}元" if isinstance(mn, (int, float)) else "N/A"
                lines.append(f"  {src_name}: 主力净额 {mn_str}")
            else:
                lines.append(f"  {src_name}: 数据暂不可用")
        lines.append(f"  说明: {result.get('note', '')}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_main_flow_compare 工具失败 [{symbol}]: {e}")
        return f"主力双源对比失败: {str(e)[:100]}"


@register_chat_tool(
    "get_delta_series",
    schema={
        "type": "function",
        "function": {
            "name": "get_delta_series",
            "description": "基于L2逐笔穿透计算秒级Delta序列(主动买-主动卖金额)及顶底背离信号。仅限A股(CN)。入参 symbol=6位A股代码如002361。先拉取THS L2全天逐笔, 再计算每秒净额、30秒平滑Delta、累计Delta、顶背离/底背离信号。用于回答「逐笔Delta分析」「有没有顶背离/底背离」「资金持续力度」等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6位A股代码, 如 002361"},
                    "market": {"type": "string", "description": "市场代码, 仅支持 CN", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="THS L2 逐笔·秒级Delta(主动买-主动卖)口径",
)
async def _tool_get_delta_series(db: Session, args: dict, user: User | None = None) -> str:
    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    if market != "CN":
        return "秒级Delta序列分析仅支持 A 股(CN)。"
    try:
        from src.core.dark_l2 import fetch_l2_ticks
        from src.core.delta_engine import compute_delta_series

        ticks = await asyncio.to_thread(fetch_l2_ticks, symbol, "thsdk")
        if not ticks:
            return f"[数据源: THS L2 逐笔] {symbol} 无逐笔数据(可能盘前或数据源异常)。"
        result = compute_delta_series(ticks, smooth_sec=30, divergence_min_sec=120)
        st = result["stats"]
        signals = result.get("signals", [])
        lines = [f"[数据源: THS L2 逐笔] {symbol} 秒级Delta序列:"]
        lines.append(f"  逐笔条数={result['ticks']}  |  时间区间={result['first_t']}~{result['last_t']}")
        lines.append(f"  主动买={st['total_buy_yuan']:,.0f}元  |  主动卖={st['total_sell_yuan']:,.0f}元  |  净额={st['net_yuan']:,.0f}元")
        lines.append(f"  Delta30峰值={st['peak_delta30']:,.0f}元  |  谷值={st['trough_delta30']:,.0f}元")
        lines.append(f"  累计Delta(末)={st['cum_net_last']:,.0f}元  |  价格区间={st['lo_price']}~{st['hi_price']}")
        if signals:
            for s in signals[:5]:
                lines.append(f"  ⚠ {s['type']} @ {s['t']}  price={s['price']}  delta30={s['delta30']:,.0f}  since={s['since']} 持续{s['streak']}s")
        else:
            lines.append("  无顶底背离信号")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_delta_series 工具失败 [{symbol}]: {e}")
        return f"秒级Delta序列分析失败: {str(e)[:100]}"


@register_chat_tool(
    "get_orderbook",
    schema={
        "type": "function",
        "function": {
            "name": "get_orderbook",
            "description": "采集THS L2盘口(20档)多快照演变分析: 托单/压单/撤单/幽灵单检测 + 订单簿失衡(OB) + 幽灵单比率。仅限A股(CN)。入参 symbol=6位A股代码如002361。采集约8个快照(间隔1.5s, 约12秒)。用于回答「盘口有没有托单压单」「有没有幽灵挂单」「订单簿是否失衡」「主力在护盘还是压制」等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6位A股代码, 如 002361"},
                    "market": {"type": "string", "description": "市场代码, 仅支持 CN", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="THS L2 盘口20档快照(托单/压单/撤单/幽灵单口径)",
)
async def _tool_get_orderbook(db: Session, args: dict, user: User | None = None) -> str:
    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    if market != "CN":
        return "盘口演变分析仅支持 A 股(CN)。"
    try:
        from src.core.main_flow_compare import _to_thsdk_symbol
        from src.core.orderbook_engine import run

        ths_code = _to_thsdk_symbol(symbol)
        if not ths_code:
            return f"无法将 {symbol} 转换为 THS 代码(仅支持6位A股代码)。"
        result = await asyncio.to_thread(run, ths_code, 8, 1.5)
        lines = [f"[数据源: THS L2 盘口] {symbol}({ths_code}) 盘口演变分析:"]
        lines.append(f"  {result['summary']}")
        events = result.get("events", [])
        if events:
            lines.append(f"  盘口事件({len(events)}条):")
            for ev in events[:10]:
                note = f"  - {ev['note']}" if ev.get("note") else ""
                lines.append(f"    [{ev['type']}] {ev['side']} 档{ev.get('price_level', '?')} @ {ev['price']} 手数变化{ev.get('delta_hands', 0):+,} {note}")
        else:
            lines.append("  无盘口事件(盘口静止或非交易时段)。")
        ob_series = result.get("ob_series", [])
        if ob_series:
            labels = [s["label"] for s in ob_series]
            lines.append(f"  订单簿失衡: {', '.join(labels)}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_orderbook 工具失败 [{symbol}]: {e}")
        return f"盘口演变分析失败: {str(e)[:100]}"


@register_chat_tool(
    "get_event_catalyst",
    schema={
        "type": "function",
        "function": {
            "name": "get_event_catalyst",
            "description": "事件驱动预期差分析: 把当日公告/新闻推理成催化信号 + 受益链 + 预期差。仅限A股(CN)。入参 symbol=6位A股代码如002361。基于当日公告做因果链推理(如停产→供给收缩→涨价→受益股), 输出催化题材/方向/置信度/受益股池/预期差(利好未反应=高预期差=潜伏价值, 利好已涨=兑现追高)。用于回答「这公告利好什么」「有哪些受益股」「预期差大不大」等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6位A股代码, 如 002361"},
                    "market": {"type": "string", "description": "市场代码, 仅支持 CN", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="当日公告→AI因果推理(预期差口径)",
)
async def _tool_get_event_catalyst(db: Session, args: dict, user: User | None = None) -> str:
    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    if market != "CN":
        return "事件催化分析仅支持 A 股(CN)。"
    try:
        from src.core.event_catalyst_engine import analyze_event_catalyst

        result = await asyncio.to_thread(analyze_event_catalyst, symbol, None)
        if not result:
            return f"{symbol} 当日无公告事件, 或 AI 推理失败(静默降级), 无法生成催化信号。"
        gap = result.get("expectation_gap") or {}
        lines = [
            f"[数据源: 当日公告→AI推理] {symbol} 事件催化与预期差:",
            f"  催化题材: {result.get('catalyst')}",
            f"  方向: {result.get('direction')} | 置信度: {result.get('confidence')}",
        ]
        pool = result.get("beneficiary_pool") or []
        if pool:
            lines.append(f"  受益链: {' / '.join(pool)}")
        if gap:
            lines.append(f"  预期差: {gap.get('level')} — {gap.get('note')}")
        if result.get("reason"):
            lines.append(f"  理由: {result['reason']}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_event_catalyst 工具失败 [{symbol}]: {e}")
        return f"事件催化分析失败: {str(e)[:100]}"


@register_chat_tool(
    "get_intent_explain",
    schema={
        "type": "function",
        "function": {
            "name": "get_intent_explain",
            "description": "主力意图 AI 解释: 对规则算法算出的主力意图结论, 用 AI 结合内外盘/拆单/筹码/位置给出「为什么」+ 置信度 + 方向(吸筹/派发/洗盘/中性)。仅限A股(CN)。入参 symbol=6位A股代码如002361。规则给结论, AI 只做解释不改变结论。用于回答「为什么说主力在吸筹」「这个主力意图怎么看」等需要解释的问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6位A股代码, 如 002361"},
                    "market": {"type": "string", "description": "市场代码, 仅支持 CN", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="主力意图规则算法+AI解释(规则给结论, AI 不改结论)",
)
async def _tool_get_intent_explain(db: Session, args: dict, user: User | None = None) -> str:
    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    if market != "CN":
        return "主力意图解释仅支持 A 股(CN)。"
    try:
        from src.core.dark_flow import compute_dark_flow
        from src.core.intent_explain import explain_main_intent

        dark = await asyncio.to_thread(compute_dark_flow, symbol)
        if not dark:
            return f"未能获取 {symbol} 的主力意图数据(可能盘前无数据)。"
        result = await asyncio.to_thread(explain_main_intent, dark, None)
        if not result:
            return f"[数据源: 主力意图规则算法] {symbol} 数据不足或 AI 解释失败(静默降级)。规则结论: {dark.get('signal', '未知')}"
        return (
            f"[数据源: 主力意图规则算法 + AI解释] {symbol}\n"
            f"  方向: {result.get('direction')} | 置信度: {result.get('confidence')}\n"
            f"  为什么: {result.get('why')}\n"
            f"  (规则结论: {dark.get('signal', '未知')})"
        )
    except Exception as e:
        logger.warning(f"get_intent_explain 工具失败 [{symbol}]: {e}")
        return f"主力意图解释失败: {str(e)[:100]}"


@register_chat_tool(
    "get_factor_ic_report",
    schema={
        "type": "function",
        "function": {
            "name": "get_factor_ic_report",
            "description": "因子 IC 归因报告: 读取因子有效性评估(IC/IR)结果, 用 AI 解读哪些因子有真实 alpha、哪些失效、哪些市态依赖, 并给出调权建议。仅限A股(CN)。入参 market 默认 CN。用于回答「哪些因子最近有效」「因子权重该怎么调」等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "market": {"type": "string", "description": "市场代码, 仅支持 CN", "default": "CN"},
                },
                "required": [],
            },
        },
    },
    caliber="因子IC/IR评估+AI归因",
)
async def _tool_get_factor_ic_report(db: Session, args: dict, user: User | None = None) -> str:
    market = args.get("market", "CN")
    if market != "CN":
        return "因子 IC 归因报告仅支持 A 股(CN)。"
    # 2026-10-21: 原实现走 LLM 归因且要 OpenAI key（实测 Missing credentials → 静默降级成
    # "样本不足"，等于废掉）。改成纯本地 Spearman IC：因子表 × 日线未来收益，零外部依赖。
    try:
        from src.core.factor_ic import compute as _compute_ic, render as _render_ic

        m = await asyncio.to_thread(_compute_ic, db)
        return _render_ic(m)
    except Exception as e:
        logger.warning(f"get_factor_ic_report 工具失败 [{market}]: {e}")
        return f"因子 IC 计算失败: {str(e)[:100]}"


# ──────────── 库优先 + 口径/时点标注(2026-10-22) ────────────
# 为什么改: 同一数字在页面(读落库快照, = 某时点)与对话(直连 TQ 客户端, = 此刻)不一样,
# 用户会看到两个"现价"却不知道哪个是几点。规则:
#   1) **能库优先的库优先**: `pricevol` 实时报价先查 `quote_snapshots` 该标的最近一行,
#      行龄 ≤ QUOTE_FRESH_SEC 直接用它(**不调 RPC**);
#   2) 库无表/无行/过期 → 回落实时 RPC, 末行同时标出库内时点与 RPC 时点
#      —— 过期快照**不许**充作实时;
#   3) 库无对应表的(板块归属/转债条款/申购日历/跟踪ETF)保持 RPC, 末行标「实时 RPC HH:MM:SS」;
#   4) 取不到就如实写「取不到(库无最新快照 / RPC 不可用)」, 不猜数、不拿其它源顶。
# 纪律: 只读 SQL(`text()` + 绑定参数 + LIMIT), 不新增表。

_CST = ZoneInfo("Asia/Shanghai")
#: 库内报价可用阈值(秒): 超过即视为过期 → 回落 RPC(1 分钟桶, 60s = 最多差一桶)。
QUOTE_FRESH_SEC = 60.0

#: `quote_snapshots` 最近一行(列名与迁移 157 一致; symbol 存裸码 + market 区分市场)。
_QUOTE_SNAPSHOT_SQL = (
    "SELECT trade_date, ts, symbol, market, name, last, open, high, low, prev_close, "
    "change_pct, volume, turnover, source FROM quote_snapshots "
    "WHERE symbol = :s AND market = :m ORDER BY trade_date DESC, ts DESC LIMIT 1"
)


def _now_cn() -> datetime:
    return datetime.now(_CST)


def _hms(when: datetime | None = None) -> str:
    """HH:MM:SS —— 实时 RPC 的取数时点。"""
    return (when or _now_cn()).strftime("%H:%M:%S")


def _minute_ts(ts: Any) -> str:
    """库内时点统一到分钟(库本身就是 1 分钟桶, 秒位无意义)。"""
    s = str(ts or "").strip()
    return s[:16] if len(s) >= 16 else (s or "时点未知")


def _caliber_db(ts: Any, table: str = "quote_snapshots", note: str = "") -> str:
    """库路径末行: 「口径：库内快照 2026-10-21 15:00（quote_snapshots）」"""
    extra = f"，{note}" if note else ""
    return f"口径：库内快照 {_minute_ts(ts)}（{table}{extra}）"


def _caliber_rpc(when: datetime | None = None) -> str:
    """RPC 路径末行: 「口径：实时 RPC 21:33:12（tdx-gateway）」"""
    return f"口径：实时 RPC {_hms(when)}（tdx-gateway）"


def _caliber_db_then_rpc(ts: Any, note: str, *, table: str = "quote_snapshots",
                         when: datetime | None = None) -> str:
    """库过期/缺行后回落 RPC —— 两个时点都写清, 让"哪个是几点"可追。"""
    return (f"口径：库内快照 {_minute_ts(ts)}（{table}，{note}）"
            f"／实时 RPC {_hms(when)}（tdx-gateway）")


def _caliber_missing(reason: str) -> str:
    return f"口径：取不到（{reason}）"


def _snapshot_symbol(code: str) -> tuple[str, str]:
    """产品/TQ 代码 → quote_snapshots 的 (symbol, market)。该表存裸码 + 市场列。"""
    c = str(code or "").strip().upper()
    if not c:
        return "", "CN"
    if "." in c:
        sym, suffix = c.rsplit(".", 1)
        return sym, {"HK": "HK", "US": "US", "IDX": "IDX", "OF": "OF"}.get(suffix, "CN")
    return c, "CN"


def _db_quote_snapshot(db: Any, code: str) -> dict[str, Any] | None:
    """该标的 `quote_snapshots` 最近一行(只读)。

    表缺失/无行/库异常一律 None(**绝不抛**), 由调用方回落 RPC —— 旧库没跑迁移也不能让对话炸。
    """
    sym, mkt = _snapshot_symbol(code)
    if not sym or db is None:
        return None
    try:
        from sqlalchemy import text

        row = db.execute(text(_QUOTE_SNAPSHOT_SQL), {"s": sym, "m": mkt}).mappings().first()
        return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001 — 无表/只读库/连接异常都按"库无快照"处理
        logger.debug("quote_snapshots 读取失败(按库无快照处理) %s.%s: %r", sym, mkt, exc)
        # PG 下语句报错会把事务置为 aborted, 同请求后续读会全挂 —— 本函数只读, 回滚安全。
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None


def _quote_age_sec(row: dict[str, Any] | None) -> float | None:
    """`quote_snapshots.ts`(CST 墙钟)距现在的秒数; 解析失败 → None(当作不新鲜)。"""
    ts = str((row or {}).get("ts") or "").strip()
    if not ts:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(ts, fmt).replace(tzinfo=_CST)
        except ValueError:
            continue
        return (_now_cn() - dt).total_seconds()
    return None


def _pricevol_rpc(codes: list[str]) -> tuple[bool, Any]:
    """实时报价 RPC 单点(独立函数 = 测试可 monkeypatch; 异常转 (False, err) 与全站降级口径一致)。"""
    return _tq_vendor_call("pricevol", codes)


def _db_first_price(db: Any, code: str) -> tuple[dict[str, Any] | None, str]:
    """报价**库优先**: 返回 (quote{close,trade_date}, 末行口径文案)。

    - 库内最近一行行龄 ≤ QUOTE_FRESH_SEC → 直接用库(**不调 RPC**);
    - 无行/过期/无表 → 回落 pricevol RPC, 口径同时标库内时点(若有)与 RPC 时点;
    - 库与 RPC 都取不到 → (None, 「取不到（…）」)。
    """
    row = _db_quote_snapshot(db, code)
    age = _quote_age_sec(row)
    if row is not None and age is not None and -5.0 <= age <= QUOTE_FRESH_SEC:
        close = row.get("last")
        if close is not None:
            quote = {"close": close, "trade_date": str(row.get("trade_date") or "")}
            return quote, _caliber_db(row.get("ts"))

    called_at = _now_cn()
    ok, payload = _pricevol_rpc([code])
    item: dict[str, Any] = {}
    if ok and isinstance(payload, dict):
        got = payload.get(code) or payload.get(str(code).rsplit(".", 1)[0])
        item = got if isinstance(got, dict) else {}
    if item.get("Now") is not None:
        quote = {"close": item.get("Now"), "trade_date": ""}
        if row is None:
            return quote, f"口径：取不到（库无最新快照）／实时 RPC {_hms(called_at)}（tdx-gateway）"
        note = (f"已过期 {age:.0f}s，不作实时" if age is not None
                else "行时点不可解析，不作实时")
        return quote, _caliber_db_then_rpc(row.get("ts"), note, when=called_at)

    if row is None:
        return None, _caliber_missing("库无最新快照 / RPC 不可用")
    note = f"库内快照已过期 {age:.0f}s" if age is not None else "库内快照时点不可解析"
    return None, _caliber_missing(f"{note} / RPC 不可用")


@register_chat_tool(
    "get_stock_chips",
    schema={
        "type": "function",
        "function": {
            "name": "get_stock_chips",
            "description": (
                "个股筹码/主力体检，用通达信客户端**现成公式**给出成本与筹码分布相关读数："
                "筹码峰成本 SSRP、市场成本 MCST、成本均线 CYC、成本价均线 AMV、筹码引力 PAV/PAVE、"
                "主力控盘 CYW、庄家抬轿 ZJTJ。"
                "用户问「这只票筹码怎么样」「主力在吸筹还是派发」「筹码峰在哪」「成本区间」时调用。"
                "注意：这是客户端公式口径的客观读数，不含买卖结论。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 002361 或 002361.SZ"},
                    "count": {"type": "integer", "description": "参与计算的日线根数，默认 250", "default": 250},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="客户端公式口径(SSRP/MCST/CYC/AMV/PAV/PAVE/CYW/ZJTJ)，非拟合值",
)
async def _tool_get_stock_chips(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.tq_chips import build_chips, normalize_code, render_text

    code = normalize_code(str(args.get("symbol") or args.get("code") or ""))
    if not code:
        return "请提供股票代码(symbol)，如 002361 或 002361.SZ。"
    count = int(args.get("count") or 250)
    # 2026-10-22 库优先: 现价先读 quote_snapshots(行龄 ≤60s 直接用), 过期/无行才回落 pricevol;
    # 末行标口径+时点, 避免"页面用落库价、对话用此刻价"两个现价。
    quote, caliber = _db_first_price(db, code)
    payload = build_chips(code, count=count, quote=quote)
    if not payload.get("ok"):
        return f"{_tq_unavailable(str(payload.get('detail') or '公式引擎不可用'))}\n{caliber}"
    return f"{render_text(payload)}\n{caliber}"


@register_chat_tool(
    "get_early_signals",
    schema={
        "type": "function",
        "function": {
            "name": "get_early_signals",
            "description": (
                "启动早期信号：用通达信客户端条件选股公式**全市场扫描**的结果，给出"
                "「放量上攻 / 温和放量上攻 / 阶段放量 / 持续放量 / 突然放量 / 下跌多日再放量上涨 /"
                "小步碎阳 / 突破长期盘整 / 四串阳 / 连续N天收阳 / 昨日底部十字星 / 价量渐低后阳包阴 /"
                "早晨之星 / 阳包阴」各自今天有多少只票满足，以及相对近 N 日基线的倍数，并给出命中清单。"
                "用户问「今天有哪些票刚启动/刚放量」「有没有突破形态」「启动早期信号」「埋伏候选」时调用。"
                "⚠️ 这是客观条件满足的家数，**不是推荐**；扫描不完整时会显式提示。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "基线窗口交易日数，默认 20", "default": 20},
                },
                "required": [],
            },
        },
    },
    caliber="客户端条件选股全市场扫描(通达信 TQ)，计数=只，基线=近 N 日均值",
)
async def _tool_get_early_signals(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.startup_signals import read_startup_signals, render_text

    days = int(args.get("days") or 20)
    payload = read_startup_signals(db, days=days)
    if not payload.get("ok"):
        return (
            "启动早期信号暂不可用：尚未扫描落库（由盘后定时任务写入，非实时）。"
            "此处**不猜数据**——需要的话先跑一次条件选股扫描。"
        )
    return render_text(payload)


# ──────────────── 数智参谋 5 工具(2026-10-18, AI 数智参谋全链路 §5) ────────────────
# 位置铁律: 必须插在 thsdk 段之前 —— core 工具是导出清单的**连续前缀**,
# 插到 thsdk 之后会排到 44+ 位, tests/test_w33_chat_registry.py 的 EXPECTED_CORE_ORDER
# index 断言直接红。实现收口在 tools_market.py(注册顺序 = MARKET_TOOLS 顺序)。
from src.agents.chat import tools_market as _tools_market  # noqa: E402  (无模块级反向依赖)

_tools_market.register_market_tools()


@register_chat_tool(
    "get_stock_dossier",
    schema={
        "type": "function",
        "function": {
            "name": "get_stock_dossier",
            "description": "【首选核心Harness】获取单只股票的全面综合客观证据档案（9维并行打包）：实时量价、均线技术面、K线形态识别(同花顺29形态+TA-Lib)、腾讯逐笔主力意图、120日底仓筹码沉淀(成本峰与获利盘)、数智决策三指标(GS多空色带与机构活跃度)、东方财富券商研报评级、近期公司公告(近15日)、利好面消息与利空风险(近7日规则筛选)。分析个股/问某股票怎么样/持仓诊断/最近有什么消息公告时必须优先调用本工具，避免盲目单点调用导致证据缺失。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码，如 600111 或 002361"},
                    "market": {"type": "string", "description": "市场代码：CN/HK/US", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="综合全景档案·量价+逐笔主力+底仓筹码+决策色带+机构研报",
)
async def _tool_get_stock_dossier(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.permissions import PERM_VIEW_QUOTE

    denied = _perm_denied(user, db, PERM_VIEW_QUOTE)
    if denied:
        return denied
    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    if not symbol:
        return "请提供股票代码(symbol)。"

    try:
        from src.agents.chat.stock_dossier_aggregator import build_stock_full_dossier, format_stock_dossier_text

        dossier = await build_stock_full_dossier(symbol, market=market, db=db, user=user)
        return format_stock_dossier_text(dossier)
    except Exception as e:
        logger.warning("获取标的全景档案失败: %s", e)
        return f"获取全景档案失败: {e}"


@register_chat_tool(
    "propose_action",
    schema={
        "type": "function",
        "function": {
            "name": "propose_action",
            "description": (
                "向用户提议一个可执行动作（不会立即生效，必须用户点确认）。"
                "kind=annotation：在K线图上加一条**水平价位线**，必须给 symbol + label + price；"
                "ts（如 2026-10-28 或 2026-10-28 10:00）只能作为附加说明，"
                "**不要只给 ts**——图上没有时间图层，只给 ts 的标注会被拒绝；"
                "可选 note(依据)；market 用 CN/SH/SZ/BJ/HK/US（默认 CN，须与代码所属交易所一致："
                "60/68 开头是 SH，00/30 开头是 SZ，43/83/87 开头是 BJ）；"
                "period 用 1m/5m/15m/30m/60m/1d/1w/1mth（月线写 1M 或 1mth，默认 1d）；"
                "kind=watchlist：把某只票加入自选，需要 symbol，可选 name/market；"
                "kind=alert：建盯盘提醒（当前仅记录，暂不执行）。"
                "只在用户明确要「标注/加自选/盯一下」时调用；不要用它代替分析结论。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["annotation", "watchlist", "alert"]},
                    "symbol": {"type": "string", "description": "6 位 A 股代码"},
                    "label": {"type": "string", "description": "标注短标签（annotation 必填，≤20 字）"},
                    "price": {"type": "number", "description": "标注价位（水平线，annotation 必须有）"},
                    "ts": {"type": "string", "description": "标注时间点（仅作附加说明；单独给 ts 会被拒绝，图上没有时间图层）"},
                    "note": {"type": "string", "description": "依据/口径说明"},
                    "name": {"type": "string", "description": "股票名称（watchlist 用）"},
                    "market": {"type": "string", "description": "市场，默认 CN"},
                    "period": {"type": "string", "description": "周期，默认 1d"},
                },
                "required": ["kind"],
            },
        },
    },
    caliber="动作提议: 落 pending 待用户确认，不直接改数据",
    requires="self",
)
async def _tool_propose_action(db: Session, args: dict, user: User | None = None) -> str:
    from src.harness.actions import ALLOWED_KINDS, ActionError, propose

    if user is None:
        return "该动作需要登录用户，当前会话没有用户身份。"
    kind = str(args.get("kind") or "").strip()
    if kind not in ALLOWED_KINDS:
        return f"不支持的 kind：{kind}（可选 {', '.join(ALLOWED_KINDS)}）"
    payload = {k: v for k, v in args.items() if k != "kind" and v not in (None, "")}
    try:
        item = propose(db, user_id=user.id, kind=kind, payload=payload)
    except ActionError as exc:
        return f"动作提议失败：{exc}"
    return (
        f"已在「待确认动作」里生成一条提议（#{item['id']}，类型 {kind}）。"
        "请提醒用户在参谋面板点确认后才会生效；不要声称已经加好或已经标注。"
    )


@register_chat_tool(
    "get_stock_report",
    schema={
        "type": "function",
        "function": {
            "name": "get_stock_report",
            "description": "获取单只股票的「自选股体检报告」：并发聚合实时行情、主力意图(逐笔)、技术面、资金流向(四档)四类数据，输出结构化 JSON(供报告模板渲染)。回答「XX股票体检一下」「XX的综合分析报告」等需要一站式多维度概览的问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6位A股代码, 如 002436"},
                    "market": {"type": "string", "description": "市场代码, 仅支持 CN", "default": "CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="行情+逐笔主力+技术面+资金四档 聚合体检",
)
async def _tool_get_stock_report(db: Session, args: dict, user: User | None = None) -> str:
    """聚合 4 个 skill 成一份体检报告(供 get_stock_report 专用 HTML 模板消费)。

    返回 JSON 字符串(而非人类可读文本), 结构:
    {"quote": str, "main_intent": str, "technical": str, "capital_flow": str}
    各字段为对应 skill 的原始文本(含 [数据源:...] 前缀), 由渲染层逐字段解析。
    任一子项失败不阻断整体, 该字段置空字符串。
    """
    import json as _json

    symbol = (args.get("symbol") or "").strip()
    market = args.get("market", "CN")
    if not symbol:
        return '{"error": "请提供股票代码(symbol)。"}'
    if market != "CN":
        return '{"error": "体检报告仅支持 A 股(CN)。"}'

    sub_args = {"symbol": symbol, "market": market}
    fields = ("get_stock_quote", "get_main_intent", "get_technical_analysis", "get_capital_flow")
    out: dict[str, str] = {}

    async def _one(name: str) -> None:
        tool = CHAT_TOOL_REGISTRY.get(name)
        if tool is None:
            out[name] = ""
            return
        try:
            res = await tool.handler(db, dict(sub_args), None)
            out[name] = res if isinstance(res, str) else str(res)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"get_stock_report 子项失败 [{name}] {symbol}: {e}")
            out[name] = ""

    await asyncio.gather(*(_one(n) for n in fields))
    return _json.dumps(out, ensure_ascii=False)


# ──────────────── thsdk 11 个高价值工具(2026-08-20 选项 C, W3.3 并入注册表) ────────────────

from src.agents.chat.tools_thsdk import (  # noqa: E402  (仅 stdlib 级依赖, 不构成循环)
    build_thsdk_tool_schemas,
    format_thsdk_tool_result,
)


async def _exec_thsdk_handler(name: str, args: dict) -> str:
    """执行 thsdk 工具本体(选项 C)。

    通过 tools_thsdk 间接调用 thsdk_l2, 工具函数内部已做降级(available=false),
    不会抛异常到用户层。同步网络调用包 to_thread 防阻塞事件循环。
    经模块对象取 THSDK_TOOL_HANDLERS, 保持对测试 monkeypatch(tt._l2 等)友好。
    """
    from src.agents.chat import tools_thsdk as _tt

    handler = _tt.THSDK_TOOL_HANDLERS.get(name)
    if handler is None:
        # 防御: 若 handler 未注册(新工具未同步 registry), 降级返回
        return f"[thsdk] 工具 {name} 尚未注册实现。"
    try:
        result = await asyncio.to_thread(handler, **_filter_tool_args(handler, args))
        return format_thsdk_tool_result(name, result)
    except Exception as e:  # noqa: BLE001
        logger.warning("thsdk 工具执行失败 %s: %s", name, e)
        return f"[thsdk] {name} 执行出错: {str(e)[:80]}"


def _make_thsdk_handler(name: str):
    async def _handler(db: Session, args: dict, user: User | None = None) -> str:
        return await _exec_thsdk_handler(name, args or {})

    return _handler


_THSDK_SCHEMA_BY_NAME = {s["function"]["name"]: s for s in build_thsdk_tool_schemas()}

_THSDK_CALIBERS = {
    "get_thsdk_news": "同花顺个股新闻(thsdk 独有源)",
    "get_thsdk_corporate_action": "同花顺公司行动(分红/送转)",
    "get_thsdk_dde": "同花顺 DDE 大单口径(与东财四档/逐笔口径不同, 不可混用)",
    "get_thsdk_hs300_constituents": "同花顺沪深300成分(官方名单口径)",
    "get_thsdk_market_data_cn_extended": "同花顺A股扩展行情(主力净流入为 THS 口径)",
    "get_thsdk_market_data_index": "同花顺指数实时行情",
    "get_thsdk_market_data_hk": "同花顺港股实时行情",
    "get_thsdk_market_data_us": "同花顺美股实时行情",
    "get_thsdk_market_data_bond": "同花顺可转债行情",
    "get_thsdk_market_data_fund": "同花顺基金/ETF行情",
    "get_wencai_enhanced": "同花顺增强版问财(自然语言选股口径)",
}

# 按注册顺序写入 = build_thsdk_tool_schemas() 的原始顺序(与旧 CHAT_TOOLS.extend 一致)
for _thsdk_name in _THSDK_SCHEMA_BY_NAME:
    register_chat_tool(
        _thsdk_name,
        schema=_THSDK_SCHEMA_BY_NAME[_thsdk_name],
        caliber=_THSDK_CALIBERS[_thsdk_name],
        requires="",
    )(_make_thsdk_handler(_thsdk_name))
del _thsdk_name


# ──────────────── 通达信客户端(TQ)独家能力工具(2026-09-25) ────────────────
# 为什么单列: 这几个接口**只有 TQ 给**(免来源/东财/同花顺没有或口径不同), vendor 里
# 早已实现却**无人调用**(ipo_info 老版只透传裸 list; kzz_info 因判错类型恒返 {})。
# 盘活它们 = 用户问「今天有什么可打新」「这只转债的强赎价/纯债价值」「票属于哪些板块」
# 时不必再靠外部源拼。全部走 tq_rpc → 客户端本地, 零外部配额。
#
# 降级口径(与全站一致): TQ 不可用 → 如实说"取不到", **禁止用其它源猜一个数字顶上**。


def _tq_unavailable(err: str) -> str:
    return (f"通达信客户端(TQ)当前取不到该数据({err})。请确认 TQ 客户端在运行; "
            f"不要用其它来源猜测替代。")


def _tq_vendor_call(fn_name: str, *a, _quiet: bool = False, **kw):
    """调 vendor 里的 TQ 封装。返回 (ok, value_or_err); 异常不外抛, 由工具如实告知。

    `_quiet`: 该次尝试**会被上层重试**(裸码 → 补 .SZ/.SH 自愈)。这类中间失败不是最终结论,
    不打 WARNING —— 否则一次**成功**的调用会在生产日志里留下「chat TQ 工具 kzz_info 失败: ...」,
    读日志的人据此误判工具坏了(2026-09-25 生产验证实录)。由重试方在全败时补一条真实警告。
    """
    try:
        from marketdata.vendors import tq as _tq

        return True, getattr(_tq, fn_name)(*a, **kw)
    except Exception as e:  # noqa: BLE001 — 工具层不因单源失败炸掉对话
        if not _quiet:
            logger.warning("chat TQ 工具 %s 失败: %s", fn_name, e)
        return False, f"{type(e).__name__}: {e}"[:160]


def _fmt_num(x, unit: str = "", zero_word: str = "未披露") -> str:
    """0/None 一律当**未披露**说清楚 —— 客户端常用 0 表示"还没出来", 不能当真实值。"""
    if x is None or x == 0:
        return zero_word
    return f"{x:g}{unit}"


def _fmt_date8(d: str) -> str:
    d = (d or "").strip()
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else (d or "未知")


@register_chat_tool(
    "get_ipo_calendar",
    schema={
        "type": "function",
        "function": {
            "name": "get_ipo_calendar",
            "description": (
                "获取新股/新债申购日历(申购日、申购价、申购代码、申购上限、发行市盈率)。"
                "用户问「今天/最近有什么新股可打新」「有没有新债申购」「打新日历」时调用。"
                "数据来自通达信客户端(TQ), 只有它给这个日历。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["new", "bond", "all"],
                        "description": "new=新股(默认) / bond=新债 / all=两者都要",
                    },
                    "only_today": {
                        "type": "boolean",
                        "description": "true=只看今天; false(默认)=今天及以后",
                    },
                },
            },
        },
    },
    caliber="通达信客户端 TQ get_ipo_info(官方申购日历; 申购价/上限为 0 = 客户端尚未披露, 非真实值)",
)
async def _tool_get_ipo_calendar(db: Session, args: dict, user: User | None = None) -> str:
    kind = str(args.get("kind") or "new").strip().lower()
    ipo_type = {"new": 0, "bond": 1, "all": 2}.get(kind, 0)
    only_today = bool(args.get("only_today"))
    ipo_date = 0 if only_today else 1
    called_at = _now_cn()
    ok, data = _tq_vendor_call("ipo_info", ipo_type, ipo_date)
    if not ok:
        return f"{_tq_unavailable(str(data))}\n{_caliber_missing('库无申购日历表 / RPC 不可用')}"
    scope = "今天" if only_today else "今天及以后"
    what = {0: "新股", 1: "新债", 2: "新股/新债"}[ipo_type]
    if not isinstance(data, list) or not data:
        return (f"通达信申购日历({scope})里没有{what}。(数据源: 通达信客户端 get_ipo_info)\n"
                f"{_caliber_rpc(called_at)}")
    lines = [f"📅 {what}申购日历(通达信, {scope}, 共 {len(data)} 条):"]
    for r in data:
        lines.append(
            f"· {r.get('name') or '?'}({r.get('code')})"
            f" 申购日 {_fmt_date8(r.get('sg_date') or '')}"
            f" | 申购价 {_fmt_num(r.get('sg_price'), ' 元', '待定')}"
            f" | 申购代码 {r.get('sg_code') or '—'}"
            f" | 申购上限 {_fmt_num(r.get('max_sg'))}"
            f" | 发行市盈率 {_fmt_num(r.get('pe_issue'), ' 倍')}"
        )
    lines.append("注: 显示『待定/未披露』= 客户端当下确实没给该字段(不是 0); 数据源=通达信 TQ。")
    lines.append(_caliber_rpc(called_at))
    return "\n".join(lines)


@register_chat_tool(
    "get_kzz_terms",
    schema={
        "type": "function",
        "function": {
            "name": "get_kzz_terms",
            "description": (
                "获取单只可转债的**条款/基础信息**: 转股价、当期利率、剩余规模、强赎触发价、"
                "回售触发价、到期日/到期价、纯债价值、评级、正股。"
                "用户问「XX转债的强赎价多少」「这个转债转股价/还剩多少规模/什么时候到期」时调用。"
                "行情(涨跌/成交)请用 get_thsdk_market_data_bond; 本工具给的是条款。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "可转债代码, 如 128136 或 128136.SZ"},
                },
                "required": ["code"],
            },
        },
    },
    caliber="通达信客户端 TQ get_kzz_info(可转债条款口径)",
)
async def _tool_get_kzz_terms(db: Session, args: dict, user: User | None = None) -> str:
    raw = str(args.get("code") or "").strip().upper()
    if not raw:
        return "请提供可转债代码(code), 如 128136 或 128136.SZ。"
    # 裸码在客户端会报 codestr error → 依次试 SZ/SH 两种后缀(自愈, 不猜死)
    cands = [raw] if "." in raw else [raw, f"{raw}.SZ", f"{raw}.SH"]
    called_at = _now_cn()
    last_err = ""
    for c in cands:
        ok, d = _tq_vendor_call("kzz_info", c, _quiet=True)  # 中间尝试不打 WARNING(可能自愈成功)
        if not ok:
            last_err = str(d)
            continue
        if isinstance(d, dict) and d:
            pairs = [
                ("转债名称", d.get("KZZName")), ("转债代码", d.get("KZZCode")),
                ("转债现价", d.get("KZZNow")), ("正股", d.get("HSCode") or d.get("HSName")),
                ("转股价", d.get("ZGPrice")), ("当期利率", d.get("CurRate")),
                ("剩余规模", d.get("RestScope")), ("强赎触发价", d.get("ForceRedeem")),
                ("回售触发价", d.get("PutBack")), ("转股日", d.get("ZGDate")),
                ("到期日", d.get("EndDate")), ("到期价", d.get("EndPrice")),
                ("纯债价值", d.get("RealValue")), ("评级", d.get("HSScore")),
            ]
            body = " | ".join(f"{k} {v}" for k, v in pairs if v not in (None, "", "0.000", "0.00"))
            return (f"🔗 可转债条款(通达信, {c}): {body or '(客户端未返回字段)'}\n"
                    f"{_caliber_rpc(called_at)}")
    if last_err:
        # 全部候选都失败才是真失败 —— 只在这里打一条, 日志与最终结论一致。
        logger.warning("chat TQ 工具 kzz_info 全部候选失败(%s): %s", "/".join(cands), last_err)
        return f"{_tq_unavailable(last_err)}\n{_caliber_missing('库无转债条款表 / RPC 不可用')}"
    return (f"通达信里查不到可转债 {raw}(试过 {'/'.join(cands)}) —— 代码是否正确或已退市?\n"
            f"{_caliber_rpc(called_at)}")


@register_chat_tool(
    "get_stock_sectors",
    schema={
        "type": "function",
        "function": {
            "name": "get_stock_sectors",
            "description": (
                "获取某只 A 股**所属的全部板块**(行业/地区/概念/风格/指数, 含各板块成分股数)。"
                "用户问「这只票属于哪些板块/什么概念/哪个行业」「它为什么跟着某题材涨」时调用 —— "
                "比外部源更全(含通达信自编概念), 可用于题材归因。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "股票代码, 如 002361 或 002361.SZ"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="通达信客户端 TQ get_relation(客户端板块归属口径, 含自编概念)",
)
async def _tool_get_stock_sectors(db: Session, args: dict, user: User | None = None) -> str:
    raw = str(args.get("symbol") or "").strip()
    if not raw:
        return "请提供股票代码(symbol), 如 002361。"
    code = raw
    if "." not in code:
        try:  # 复用产品统一的代码转换(含 92/4/8 → BJ, 6/9/5 → SH)
            from marketdata.symbol import Market, Symbol

            c = Symbol(code=code, market=Market.CN)
            from marketdata.vendors.tq import to_tq_code

            code = to_tq_code(c) or code
        except Exception:  # noqa: BLE001 — 转换失败就按原样试
            code = raw
    called_at = _now_cn()
    ok, v = _tq_vendor_call("relation", code)
    if not ok:
        return f"{_tq_unavailable(str(v))}\n{_caliber_missing('库无板块归属表 / RPC 不可用')}"
    if not isinstance(v, list):
        return (f"通达信没给出 {code} 的板块归属(可能非 A 股代码)。\n{_caliber_rpc(called_at)}")
    rows = [r for r in v if isinstance(r, dict)]
    if not rows:
        return (f"通达信没给出 {code} 的板块归属(可能非 A 股代码)。\n{_caliber_rpc(called_at)}")
    by_type: dict[str, list[str]] = {}
    for r in rows:
        name = str(r.get("BlockName") or "").strip()
        btype = str(r.get("BlockType") or "其它").strip()
        if name:
            by_type.setdefault(btype, []).append(name)
    order = ["行业", "概念", "地区", "风格", "指数"]
    lines = [f"🧩 {code} 所属板块(通达信, 共 {len(rows)} 个):"]
    for t in order + [k for k in by_type if k not in order]:
        names = by_type.get(t)
        if names:
            lines.append(f"· {t}: " + "、".join(names))
    # 库无板块归属表(通达信自编概念只在客户端) → 保持 RPC, 末行标时点。
    lines.append(_caliber_rpc(called_at))
    return "\n".join(lines)


# ─────────── 第二批 TQ 工具(2026-09-25): 证券检索 / 指数ETF / 客户端喂数据 ───────────
# 这三个 vendor 封装**早已存在且结构正确**(与 kzz_info 不同, 无需修), 只是无人调用。
# 契约与实测见 skills/finance/tq-capability-audit/references/api-contracts.md。

#: 通达信代码后缀 → 市场中文名(检索结果跨市场, 必须标出来源, 否则 03750.HK 会被当 A 股)。
_TQ_SUFFIX_CN = {
    "SZ": "深市", "SH": "沪市", "BJ": "北交所", "HK": "港股",
    "OF": "场外基金", "CSI": "中证指数", "CFF": "中金所", "US": "美股",
}


def _tq_market_cn(code: str) -> str:
    return _TQ_SUFFIX_CN.get(code.rsplit(".", 1)[-1].upper(), "") if "." in code else ""


# ── 本地全市场名称缓存(库优先的"库") ──
# 为什么用它: 「把名字换成代码」这件事落库数据里就有(全市场代码-名称)，不必每次打 TQ 客户端;
# 客户端只在缓存没命中时才问(转债/场外基金/指数等缓存没有的品种仍靠它)。

def _local_stock_rows() -> list[dict]:
    """读本地全市场名称缓存(`stock_list.get_stock_list()`)。

    缓存文件不存在时**不**触发 `refresh_stock_list()` 的全量刷新 —— 聊天路径不许为一次
    检索去打外部源并阻塞数秒; 无缓存就当"库无"回落 RPC, 与全站"取不到就回落"一致。
    """
    from src.collectors import stock_list as _sl

    try:
        path = _sl._cache_file() if hasattr(_sl, "_cache_file") else getattr(_sl, "CACHE_FILE", "")
        if not path or not os.path.exists(path):
            return []
        return _sl.get_stock_list() or []
    except Exception as exc:  # noqa: BLE001 — 缓存读坏了不是用户的问题, 回落 RPC
        logger.debug("本地名称缓存不可用(回落 RPC): %r", exc)
        return []


def _cache_stamp() -> str:
    """缓存落盘时点(库路径的时点标注); 取不到 → 空串, 由调用方写「时点未知」。"""
    from src.collectors import stock_list as _sl

    try:
        path = _sl._cache_file() if hasattr(_sl, "_cache_file") else getattr(_sl, "CACHE_FILE", "")
        return datetime.fromtimestamp(os.stat(path).st_mtime, _CST).strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return ""


def _code_for_display(symbol: str, market: str) -> str:
    """缓存里的裸码 + 市场 → 带后缀代码(与 TQ 检索结果同形, 免得两个入口两种写法)。"""
    sym = str(symbol or "").strip().upper()
    mkt = str(market or "CN").strip().upper()
    if not sym:
        return ""
    if mkt in ("HK", "US", "OF", "CSI", "CFF"):
        return f"{sym}.{mkt}"
    try:
        from marketdata.symbol import Market, Symbol
        from marketdata.vendors.tq import to_tq_code

        return to_tq_code(Symbol(code=sym, market=Market.CN)) or sym
    except Exception:  # noqa: BLE001 — 包不可用就按代码段兜底(6/9/5→SH, 4/8/92→BJ, 其余 SZ)
        if sym.startswith(("92", "4", "8")):
            return f"{sym}.BJ"
        if sym.startswith(("6", "9", "5")):
            return f"{sym}.SH"
        return f"{sym}.SZ"


def _local_match(keyword: str, limit: int = 15) -> list[dict]:
    """精确(代码/名称) → 代码前缀 → 名称包含 的顺序匹配, 去重后取前 limit 条。"""
    q = str(keyword or "").strip().upper()
    stocks = _local_stock_rows()
    if not q or not stocks:
        return []
    bare = q.rsplit(".", 1)[0] if "." in q else q
    exact_code: list[dict] = []
    exact_name: list[dict] = []
    prefix: list[dict] = []
    contains: list[dict] = []
    for s in stocks:
        if not isinstance(s, dict):
            continue
        sym = str(s.get("symbol") or "").strip().upper()
        name = str(s.get("name") or "").strip().upper()
        if not sym or not name:
            continue
        if sym == bare:
            exact_code.append(s)
        elif name == q:
            exact_name.append(s)
        elif sym.startswith(bare):
            prefix.append(s)
        elif q in name:
            contains.append(s)
    out: list[dict] = []
    seen: set[tuple] = set()
    for bucket in (exact_code, exact_name, prefix, contains):
        for s in bucket:
            key = (s.get("market"), s.get("symbol"))
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "code": _code_for_display(str(s.get("symbol")), str(s.get("market") or "CN")),
                "name": s.get("name"),
            })
            if len(out) >= limit:
                return out
    return out


@register_chat_tool(
    "search_symbols",
    schema={
        "type": "function",
        "function": {
            "name": "search_symbols",
            "description": (
                "按名称/简称/拼音/代码检索证券, **跨市场**(A股/北交所/港股/场外基金/可转债)。"
                "用于把用户说的名字换成带市场的代码, 或确认某个名字对应哪些标的。"
                "注意: 同名可能跨市场(如宁德时代 A股 300750.SZ 与港股 03750.HK), "
                "结果里已标出市场, 选错市场会查不到行情。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "名称/简称/拼音/代码片段, 如 宁德时代、002361、立讯转债",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    caliber="通达信客户端 TQ get_match_stkinfo(证券检索口径, 覆盖 A/北交所/港股/场外基金/转债)",
)
async def _tool_search_symbols(db: Session, args: dict, user: User | None = None) -> str:
    kw = str(args.get("keyword") or "").strip()
    if not kw:
        return "请提供检索关键词(keyword), 如 宁德时代 / 002361 / 立讯转债。"
    # 2026-10-22 库优先: 全市场代码-名称本地缓存先做精确+模糊匹配, 命中就不打 TQ;
    # 缓存无命中(或没有缓存)才回落 match_stkinfo(客户端口径, 覆盖转债/场外基金/指数)。
    try:
        hits = await asyncio.to_thread(_local_match, kw)
    except Exception as exc:  # noqa: BLE001 — 匹配失败只影响"能不能少打一次 RPC"
        logger.debug("本地名称缓存匹配失败(回落 RPC): %r", exc)
        hits = []
    if hits:
        lines = [f"🔍 本地名称缓存检索「{kw}」(共 {len(hits)} 条):"]
        for r in hits:
            code = str(r.get("code") or "")
            mkt = _tq_market_cn(code)
            lines.append(f"· {r.get('name') or '?'} {code}" + (f"({mkt})" if mkt else ""))
        stamp = _cache_stamp()
        lines.append(
            _caliber_db(stamp, "本地名称缓存 stock_list_cache.json", "全市场代码-名称")
            if stamp else "口径：库内快照 时点未知（本地名称缓存 stock_list_cache.json）"
        )
        return "\n".join(lines)

    called_at = _now_cn()
    ok, v = _tq_vendor_call("match_stkinfo", kw)
    if not ok:
        return (f"{_tq_unavailable(str(v))}\n"
                f"{_caliber_missing('本地名称缓存无命中 / RPC 不可用')}")
    rows = [r for r in (v or []) if isinstance(r, dict) and r.get("Code")]
    if not rows:
        return (f"通达信里没有匹配「{kw}」的证券(名称/代码是否正确?)。\n"
                f"{_caliber_rpc(called_at)}")
    lines = [f"🔍 通达信检索「{kw}」(共 {len(rows)} 条):"]
    for r in rows[:15]:
        code = str(r.get("Code") or "")
        mkt = _tq_market_cn(code)
        lines.append(f"· {r.get('Name') or '?'} {code}" + (f"({mkt})" if mkt else ""))
    if len(rows) > 15:
        lines.append(f"(仅显示前 15 条, 共 {len(rows)} 条)")
    lines.append(_caliber_rpc(called_at))
    return "\n".join(lines)


@register_chat_tool(
    "get_index_etfs",
    schema={
        "type": "function",
        "function": {
            "name": "get_index_etfs",
            "description": (
                "查跟踪某个指数的 ETF 列表: 现价、IOPV(参考净值)、**折溢价率**、规模(亿元)。"
                "用于 ETF 选择、规模比较、以及看某只 ETF 当日是溢价还是折价。"
                "指数代码需带市场后缀, 如 000300.SH(沪深300) / 399006.SZ(创业板指) / 950162.CSI。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index_code": {
                        "type": "string",
                        "description": "指数代码, 如 000300.SH、399006.SZ、950162.CSI(裸码会自动补后缀试)",
                    },
                },
                "required": ["index_code"],
            },
        },
    },
    caliber="通达信客户端 TQ get_trackzs_etf_info(跟踪指数 ETF 口径: IOPV/规模/净额)",
)
async def _tool_get_index_etfs(db: Session, args: dict, user: User | None = None) -> str:
    raw = str(args.get("index_code") or "").strip().upper()
    if not raw:
        return "请提供指数代码(index_code), 如 000300.SH / 399006.SZ / 950162.CSI。"
    # 裸码不知市场 → 依次试后缀(自愈; 中间失败不打 WARNING, 逻辑同 get_kzz_terms)
    cands = [raw] if "." in raw else [f"{raw}.SH", f"{raw}.SZ", f"{raw}.CSI", raw]
    called_at = _now_cn()
    rows: list[dict] = []
    used = ""
    for c in cands:
        ok, v = _tq_vendor_call("trackzs_etf", c, _quiet=True)
        if ok and isinstance(v, list):
            got = [r for r in v if isinstance(r, dict) and r.get("Code")]
            if got:
                rows, used = got, c
                break
    if not rows:
        return (f"通达信里没有指数「{raw}」的跟踪 ETF(试过 {'/'.join(cands)}) —— "
                f"指数代码或后缀是否正确?\n{_caliber_rpc(called_at)}")

    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    enriched: list[tuple[float, dict, float | None]] = []
    for r in rows:
        px, iopv, sz = _f(r.get("NowPrice")), _f(r.get("IOPV")), _f(r.get("Sz"))
        # 折溢价率 = (现价 - IOPV) / IOPV; IOPV 缺失/为 0 时不给数字, 不编
        prem = ((px - iopv) / iopv * 100) if (px is not None and iopv) else None
        enriched.append((sz if sz is not None else -1.0, r, prem))
    enriched.sort(key=lambda t: t[0], reverse=True)
    lines = [f"📊 跟踪 {used} 的 ETF(通达信, 共 {len(rows)} 只, 按规模降序):"]
    for sz, r, prem in enriched[:20]:
        parts = [f"{r.get('Name') or '?'}({r.get('Code')})"]
        if r.get("Sz") not in (None, ""):
            parts.append(f"规模 {r['Sz']} 亿")
        if r.get("NowPrice") not in (None, ""):
            parts.append(f"现价 {r['NowPrice']}")
        if r.get("IOPV") not in (None, ""):
            parts.append(f"IOPV {r['IOPV']}")
        if prem is not None:
            parts.append(f"折溢价 {prem:+.2f}%")
        lines.append("· " + " | ".join(parts))
    if len(rows) > 20:
        lines.append(f"(仅显示前 20 只, 共 {len(rows)} 只)")
    lines.append(_caliber_rpc(called_at))
    return "\n".join(lines)


#: download_file 的 down_type 中文名(与客户端 Msg 对齐)。
_TQ_DOWN_NAMES = {
    1: "十大股东数据文件", 2: "ETF 申赎清单文件", 3: "最近舆情信息文件",
    4: "股票综合信息文件", 5: "经营分析数据文件",
}


@register_chat_tool(
    "download_client_data",
    schema={
        "type": "function",
        "function": {
            "name": "download_client_data",
            "description": (
                "让**通达信客户端**下载指定数据文件(落客户端本地 .\\PYPlugins\\data)。"
                "这是给客户端喂数据的前置动作, **不是取数接口** —— 返回只有客户端的执行回执, "
                "数据本身本服务读不到。用途: 需要客户端侧已有某类数据时(如经营分析/十大股东)先触发下载。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "data_type": {
                        "type": "integer",
                        "description": "1 十大股东 / 2 ETF申赎清单 / 3 最近舆情 / 4 股票综合信息 / 5 经营分析数据",
                        "enum": [1, 2, 3, 4, 5],
                    },
                    "stock_code": {
                        "type": "string",
                        "description": "证券代码(类型 1/2/5 需要), 如 688318.SH",
                    },
                    "date": {
                        "type": "string",
                        "description": "日期 YYYYMMDD(类型 1/2/5 需要, 客户端按该日期所在年度下载), 如 20260924",
                    },
                },
                "required": ["data_type"],
            },
        },
    },
    caliber="通达信客户端 TQ download_file(客户端数据文件下载口径, 非取数接口)",
)
async def _tool_download_client_data(db: Session, args: dict, user: User | None = None) -> str:
    try:
        dt = int(args.get("data_type") or 0)
    except (TypeError, ValueError):
        dt = 0
    if dt not in _TQ_DOWN_NAMES:
        return ("请提供 data_type: 1 十大股东 / 2 ETF申赎清单 / 3 最近舆情 / "
                "4 股票综合信息 / 5 经营分析数据。")
    name = _TQ_DOWN_NAMES[dt]
    code = str(args.get("stock_code") or "").strip()
    day = str(args.get("date") or "").strip()
    # 文档标这 3 项必选, 客户端也按「日期所在年度」取数 —— 缺参就明说, 不替它猜
    if dt in (1, 2, 5):
        if not day:
            return f"{name}需要指定日期(date, 形如 20260924) —— 客户端按该日期所在年度下载。"
        if not code:
            return f"{name}需要指定证券代码(stock_code, 如 688318.SH)。"
    ok, v = _tq_vendor_call("download_file", stock_code=code, down_time=day, down_type=dt)
    if not ok:
        return _tq_unavailable(str(v))
    msg = str(v.get("Msg") or "").strip() if isinstance(v, dict) else ""
    return "\n".join([
        f"📥 已触发客户端下载「{name}」" + (f" —— 客户端返回: {msg}" if msg else ""),
        "注意: 文件下载在**通达信客户端本地**(.\\PYPlugins\\data), 本服务读不到内容; "
        "这是给客户端喂数据的前置动作, 不是取数结果。",
    ])

@register_chat_tool(
    "remember_preference",
    schema={
        "type": "function",
        "function": {
            "name": "remember_preference",
            "description": (
                "当用户明确表达长期偏好或规则时（如「记住我只做主板」「以后资金流别用东财方向位」），"
                "把这句话存为长期记忆，下次会话会自动带上。只存用户明确说的，禁止自行推断偏好。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要记住的偏好/规则原文（≤500字）"},
                    "kind": {"type": "string", "enum": ["preference", "rule", "fact", "skill"],
                             "description": "preference=偏好 / rule=规则 / fact=事实 / skill=流程"},
                },
                "required": ["content"],
            },
        },
    },
    caliber="长期记忆(用户本人确认, 存 copilot_memories)",
)
async def _tool_remember_preference(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.copilot_memory import remember

    content = str(args.get("content") or "").strip()
    if len(content) < 2:
        return "没有可记的内容（content 为空）。"
    kind = str(args.get("kind") or "preference")
    row = remember(db, getattr(user, "id", None), kind=kind, content=content,
                   source="ai", weight=7)
    if row is None:
        return "记忆写入失败（不影响本次回答），可以稍后再试。"
    return f"已记住（#{row['id']}）：{row['content']}"


@register_chat_tool(
    "create_daily_task",
    schema={
        "type": "function",
        "function": {
            "name": "create_daily_task",
            "description": (
                "当用户要求「每天/每个交易日某时点」自动推送内容时使用，例如"
                "「每天9:25帮我看竞价」「收盘后给我小结」。"
                "会生成一条待确认动作，用户确认后才真正生效（不会立刻下单或改持仓）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["auction_digest", "daily_brief"],
                             "description": "auction_digest=竞价摘要 / daily_brief=收盘小结"},
                    "at_hhmm": {"type": "string", "description": "执行时间 HH:MM，如 09:25"},
                    "symbols": {"type": "array", "items": {"type": "string"},
                                "description": "指定标的；不传则用用户自选"},
                },
                "required": ["kind", "at_hhmm"],
            },
        },
    },
    caliber="会话定时任务(propose→确认→落库 copilot_schedules)",
)
async def _tool_create_daily_task(db: Session, args: dict, user: User | None = None) -> str:
    from src.harness.actions import ActionError, propose

    kind = str(args.get("kind") or "").strip()
    at = str(args.get("at_hhmm") or "").strip()
    if kind not in ("auction_digest", "daily_brief"):
        return "kind 只能是 auction_digest（竞价摘要）或 daily_brief（收盘小结）。"
    if not at:
        return "请给出执行时间 at_hhmm，例如 09:25。"
    try:
        row = propose(
            db, user_id=getattr(user, "id", None), kind="schedule",
            payload={"kind": kind, "at_hhmm": at,
                     "symbols": [str(x) for x in (args.get("symbols") or [])]},
        )
    except ActionError as e:
        return f"无法创建定时任务：{e}"
    label = "竞价摘要" if kind == "auction_digest" else "收盘小结"
    return (f"已生成待确认动作 #{row['id']}：{label} 每天 {at} 推送。"
            f"请在面板「待确认动作」里点确认后才会生效。")


@register_chat_tool(
    "get_intraday_profile",
    schema={
        "type": "function",
        "function": {
            "name": "get_intraday_profile",
            "description": (
                "分时走势画像：日内位置、均价线(VWAP)上下关系、分时段量能分布、"
                "最大放量时点、尾盘量价、外盘/内盘(主动买卖)、五档买卖压比、量比；"
                "并附「分钟资金」（逐笔聚合的每分钟主动买卖/大单净额）与「撤单趋势」（自采样时序）。"
                "问「分时怎么样/是不是放量上攻/尾盘有没有资金/盘口压不压」时先调这个，"
                "它给的是可复算的事实，不要凭K线图猜分时。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位代码，如 002050"},
                    "market": {"type": "string", "enum": ["CN", "HK", "US"], "description": "默认 CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="分时=腾讯分钟线差分 · 内外盘/五档=腾讯qt · 缺失写「未取到」不推断",
)
async def _tool_get_intraday_profile(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.intraday_profile import build as _build_intraday

    symbol = str(args.get("symbol") or "").strip().upper()
    market = str(args.get("market") or "CN").strip().upper() or "CN"
    if not symbol:
        return "请提供股票代码(symbol)，如 002050。"
    if market == "CN" and not (len(symbol) == 6 and symbol.isdigit()):
        return f"代码格式不对：{symbol}（A 股需 6 位数字）"
    import asyncio as _asyncio

    try:
        return await _asyncio.to_thread(_build_intraday, symbol, market, db)
    except Exception as exc:  # noqa: BLE001 - 源故障要说清，不许编分时
        return f"[分时画像] 取数失败：{type(exc).__name__}: {exc}——不做推断。"


@register_chat_tool(
    "get_trend_profile",
    schema={
        "type": "function",
        "function": {
            "name": "get_trend_profile",
            "description": (
                "多周期走势画像：5/20/60/120 日涨跌幅、阶段高低区间与当前位置、MA5/10/20/60 数值与斜率、"
                "量能节奏（20 日均量/今日量比/放量天数）、相对沪深300 的 20 日强弱。"
                "回答「这票前期怎么走的 / 现在在什么位置 / 趋势有没有转」时先调它，"
                "不要只凭一句「空头排列」下结论。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位代码，如 002050"},
                    "market": {"type": "string", "enum": ["CN", "HK", "US"], "description": "默认 CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="PG 前复权日线 · 收盘价口径 · 样本不足写「样本不足」不硬算",
)
async def _tool_get_trend_profile(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.trend_profile import build as _build_trend

    symbol = str(args.get("symbol") or "").strip().upper()
    market = str(args.get("market") or "CN").strip().upper() or "CN"
    if not symbol:
        return "请提供股票代码(symbol)，如 002050。"
    import asyncio as _asyncio

    try:
        return await _asyncio.to_thread(_build_trend, symbol, market)
    except Exception as exc:  # noqa: BLE001
        return f"[多周期走势] 取数失败：{type(exc).__name__}——不做推断。"


@register_chat_tool(
    "get_stock_factors",
    schema={
        "type": "function",
        "function": {
            "name": "get_stock_factors",
            "description": (
                "个股量化因子：20/60 日动量、20 日年化波动、量能 z、120 日区间位置，"
                "每项带**同日全市场横截面分位**与 20 日变化（历史够时）。"
                "回答「这票强弱在什么水平 / 波动大不大 / 是不是放量」时用它，"
                "别用涨跌幅代替因子分位。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位代码，如 002050"},
                    "market": {"type": "string", "enum": ["CN"], "description": "默认 CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="stock_factor_daily 日快照 · 横截面分位同日排名 · 缺值写「暂无」不填近似",
)
async def _tool_get_stock_factors(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.stock_factors import build as _build_factors

    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return "请提供股票代码(symbol)，如 002050。"
    out = _build_factors(db, symbol, "CN")
    # 2026-10-22: 因子的「分位」单独没信息量，必须配「这个因子现在还管不管用」。
    # 快照读库（毫秒级），没有就如实不说 —— 不现算、不编。
    try:
        from src.core.factor_ic_history import render_latest as _render_ic

        _ic_txt = _render_ic(db)
        if _ic_txt:
            out = f"{out}\n\n{_ic_txt}"
    except Exception:  # noqa: BLE001  IC 是增强项，失败不影响因子分位本身
        pass
    return out


@register_chat_tool(
    "get_scenario_matrix",
    schema={
        "type": "function",
        "function": {
            "name": "get_scenario_matrix",
            "description": (
                "情景规则矩阵：按均线/筹码带/区间高低算出关键位，给三档情景（走强/震荡/走弱），"
                "每档含触发条件、目标位、应对与失效条件。回答「后面怎么应对 / 关键位在哪 / 破了怎么办」"
                "时用它，它只给条件-反应，不预测方向。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位代码，如 002050"},
                    "market": {"type": "string", "enum": ["CN"], "description": "默认 CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="均线+筹码带+区间高低 → 关键位 · 三档情景 · 不预测方向",
)
async def _tool_get_scenario_matrix(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.scenario_compose import build_for_tool as _build_scenarios

    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return "请提供股票代码(symbol)，如 002050。"
    import asyncio as _asyncio

    try:
        return await _asyncio.to_thread(_build_scenarios, db, symbol, "CN", user=user)
    except Exception as exc:  # noqa: BLE001
        return f"[情景矩阵] 取数失败：{type(exc).__name__}——不硬编情景。"


# ──────────────── 交易模式画像(2026-10-22) ────────────────
# 位置: core 段末尾(数据域目录工具之前)。core 工具是连续前缀(EXPECTED_CORE_ORDER
# index 断言), 新增工具不得插到 core 段中间。
#
# 口径(**关键**): 本工具的 "本人胜率" 只来自 users.shadow_profile_json(交割单聚合画像),
# decision_log 是**系统生成的信号台账**, 衡量信号池质量, 不是本人战绩 —— 两者在本工具里
# 物理隔离(strengths/weaknesses 只用前者, 后者单独在 signal_ledger 段)。
@register_chat_tool(
    "get_trader_playbook",
    schema={
        "type": "function",
        "function": {
            "name": "get_trader_playbook",
            "description": (
                "用户交易模式画像：基于本人交割单聚合画像(回合数、回合胜率、持仓中位/P75 周期、"
                "市场范围、行为短板)给出**带样本量与置信度**的长项/短板，附系统信号台账的"
                "信号池质量分桶(标注为系统信号，非本人成交)。"
                "回答「我的交易风格如何」「我擅长什么/有什么短板」「胜率多少」「一般拿多久」时用它。"
                "证据不足时只说「样本不足」，不含买卖建议。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    caliber="本人交割单聚合画像(胜率/持仓周期/规则)+ 系统信号台账分桶(信号池质量, 非本人成交)",
    requires="self",
)
async def _tool_get_trader_playbook(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.trader_playbook import build as _build_playbook, to_json as _to_json

    if user is None:
        return "该工具需要登录用户（读取本人交割单画像），当前会话没有用户身份。"
    uid = getattr(user, "id", None)
    if not uid:
        return "未取到用户身份，无法读取本人交易画像。"
    try:
        playbook = await asyncio.to_thread(_build_playbook, db, uid)
    except Exception as exc:  # noqa: BLE001 —— 熔断: 不拖垮对话
        logger.warning(f"get_trader_playbook 工具失败: {exc}")
        return f"[交易模式画像] 取数失败：{type(exc).__name__}——不编画像。"
    return _to_json(playbook)


# ──────────────── 数据域目录工具(2026-10-21, 数据域架构 P0) ────────────────
# 位置: 追加在导出清单**最末**。core 工具是连续前缀(EXPECTED_CORE_ORDER index 断言),
# 新增域工具不得插到 core 段中间; 排在尾部既不打乱既有顺序, 也便于比对工具总数。
# 实现收口在 tools_catalog.py(注册顺序 = 本模块 register_catalog_tools 内的顺序)。
from src.agents.chat import tools_catalog as _tools_catalog  # noqa: E402  (仅 stdlib 级依赖)

_tools_catalog.register_catalog_tools()


# ──────────────── 新分析维度提案(2026-10-22, P0 采集层) ────────────────
# 位置: 注册表最末。
#
# **两段式暴露(2026-10-22 晚更新)**:
#   ① `fallback=False` —— **绝不进「无配方命中 → 全量工具」兜底路径**。那条路径同时服务
#      闲聊/问价, 把采集工具放进去等于让模型在闲聊里自由写库(原先 schema=None 挡的正是这个)。
#   ② schema 就位 + 只挂进 `deep_diagnosis` —— 只在高信息密度的深度诊断里可见, 那里才有
#      「用户要的维度确实取不到」的真实场景。配方白名单本身就是意图门控。
#   所以「模型随手记」的风险由**路由**关掉, 不由「模型看不见」关掉 —— 后者代价是采集链路
#   永远没有入口(工具存在但永远不会被调用, 提案表恒空)。
#
# 为什么还注册成工具(而不是只留一个 core 函数):
#   1. 工具名进注册表 → 有统一的 schema/caliber/权限契约;
#   2. chat.py 的 _execute_tool 按名字分发, 不用改 chat.py(它有 2300 行护栏);
#   3. 口径标签(caliber)随注册走, 治理页/轨迹里能看出"这次调用只是记录, 没取数"。
@register_chat_tool(
    "note_new_dimension",
    schema={
        "type": "function",
        "function": {
            "name": "note_new_dimension",
            "description": (
                "记录用户明确提出、但现有工具与数据确实拿不到的分析维度，供人工审核后决定是否补能力。"
                "仅在本轮已确认无法通过其他工具回答该维度时调用，且同一轮最多一次。"
                "不要为已有工具能取到的维度调用；闲聊、问价、寒暄时不要调用。"
                "该工具只记录，不取数、不返回任何分析内容。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dimension": {
                        "type": "string",
                        "description": "维度名，2~40 字，名词短语，如「股东户数变化」「龙虎榜席位风格」",
                    },
                    "reason": {
                        "type": "string",
                        "description": "为什么需要它：用户要的是什么，现有哪块数据/工具覆盖不到",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "可选，本轮讨论的标的代码，如 600111",
                    },
                },
                "required": ["dimension", "reason"],
            },
        },
    },
    caliber="只记录不生效: 写入 harness_proposals(用户提出的新分析维度), 不取数、不影响作答",
    requires="self",
    fallback=False,
    charges_budget=False,
)
async def _tool_note_new_dimension(db: Session, args: dict, user: User | None = None) -> str:
    """记一条"用户想要但配方里没有"的分析维度。

    入参: dimension(必填) / reason(必填) / symbol?(可选) / conversation_id?(可选)。
    返回: created / dimension / seen_count / status 四项(记录结果, 不改任何分析)。

    重复提同一维度 → 不插新行, 只把 seen_count 加一(返回 created=false)。
    """
    from src.core.harness_proposals import propose
    from src.harness.context import REQ_RECIPE_LABEL

    dimension = str((args or {}).get("dimension") or "").strip()
    reason = str((args or {}).get("reason") or "").strip()
    if not dimension or not reason:
        return "缺少参数：dimension（维度名）与 reason（为什么需要它）都必须给。"

    symbol = str((args or {}).get("symbol") or "").strip() or None
    conv_id = (args or {}).get("conversation_id")
    try:
        conv_id = int(conv_id) if conv_id not in (None, "") else None
    except (TypeError, ValueError):
        conv_id = None

    try:
        row = propose(
            db,
            getattr(user, "id", None),
            dimension=dimension,
            reason=reason,
            symbol=symbol,
            conversation_id=conv_id,
            recipe=(REQ_RECIPE_LABEL.get() or None),  # 当时命中了哪份配方(标签, 非 id)
        )
    except ValueError as exc:
        return f"提案未记录：{exc}"
    except Exception as exc:  # noqa: BLE001 - 采集是旁路, 失败不许影响本次作答
        logger.warning("[harness-proposals] 记录失败(忽略): %s", exc)
        return "提案记录失败（不影响本次回答）。"

    # 返回四项结果(机器可读): created/dimension/seen_count/status
    return _result_json(row)


def _result_json(row: dict) -> str:
    """四项结果(created/dimension/seen_count/status) —— 机器可读的 JSON 串。

    注意: 这个工具只**记录**, 不返回任何分析内容 —— 收到它不代表这次作答多了维度。
    """
    import json

    payload = {
        "created": bool(row.get("created")),
        "dimension": row.get("dimension"),
        "seen_count": int(row.get("seen_count") or 0),
        "status": row.get("status"),
    }
    return json.dumps(payload, ensure_ascii=False)


# ──────────────── 盘前分析底稿(2026-10-23, B 支线: 盘前 Agent 接线) ────────────────
# 位置: 追加在注册表**最末** —— core 工具是导出清单的连续前缀(EXPECTED_CORE_ORDER index
# 断言), 新工具不得插到 core/thsdk 段中间, 只能排尾部。
# fallback=False: 盘前底稿只在盘前策略官配方/明确的盘前场景有意义, 不进「无配方命中 →
# 全量工具」兜底路径(那条路径服务闲聊/问价, 取数类工具混进去会污染对话)。
@register_chat_tool(
    "get_premarket_brief",
    schema={
        "type": "function",
        "function": {
            "name": "get_premarket_brief",
            "description": (
                "获取用户最新的盘前分析底稿（当日底稿；指定 date 参数取该日底稿）："
                "闸门得分与仓位上限、题材主线强度、观察计划（触发/止损位）、关键价位。"
                "回答「今天盘前分析」「今日观察什么」「闸门多少分」等盘前相关问题时调用。"
                "历史记录会显式标注「非今日」，引用时注意基准日。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "底稿日期 YYYY-MM-DD，可选；不填取最新一条",
                        "default": "",
                    },
                },
                "required": [],
            },
        },
    },
    caliber="盘前分析·当日底稿",
    requires="self",
    fallback=False,
)
async def _tool_get_premarket_brief(db: Session, args: dict, user: User | None = None) -> str:
    from src.web.api import premarket as _premarket

    if user is None:
        return "该工具需要登录用户（读取本人盘前分析底稿），当前会话没有用户身份。"
    date = str((args or {}).get("date") or "").strip() or None
    try:
        # 同步 DB 读取放线程池, 不阻塞事件循环(与全站 to_thread 口径一致)
        return await asyncio.to_thread(_premarket.render_brief, db, user, date)
    except Exception as exc:  # noqa: BLE001 — 读取失败不拖垮对话
        logger.warning("get_premarket_brief 工具失败: %s", exc)
        return f"盘前分析底稿获取失败: {str(exc)[:80]}"



# ──────────────── 四档资金结构 / 委托粒度(2026-10-23, exday 能力产品化) ────────────────
# 位置: 追加在注册表**最末**(同 get_premarket_brief 的理由: core 工具是导出清单的连续
# 前缀, 新工具只能排尾部)。
#
# 口径关键: `tdx_exday4` = 通达信客户端 `get_exday_data` 的**按单笔成交金额分四档**口径,
# 与 `get_capital_flow`(东财 eastmoney4)、`get_main_intent`(腾讯逐笔 tick) **互不可换算**,
# 三者同屏必须各带标签。档位阈值官方未公开, 为推断值(见 exday_structure.TIER_THRESHOLDS_HINT)。
@register_chat_tool(
    "get_fund_structure",
    schema={
        "type": "function",
        "function": {
            "name": "get_fund_structure",
            "description": (
                "获取个股**四档资金结构**（通达信客户端口径）：特大/大/中/小 四档的买入额与"
                "主动买卖净额、主动买占比、散户化率、特大单占比、主力(特大+大)主动净额、"
                "筹码迁移(大单净−小单净)。回答「资金结构怎么样」「大单还是小单在买」"
                "「筹码是往大资金集中还是派发」「散户多不多」这类问题时用它。"
                "与东财四档(get_capital_flow)、腾讯逐笔(get_main_intent)口径不同，不可混用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位代码，如 002361"},
                    "market": {"type": "string", "enum": ["CN"], "description": "默认 CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="通达信客户端四档口径(tdx_exday4)：按单笔成交金额分档，与东财四档/腾讯逐笔不可换算",
)
async def _tool_get_fund_structure(db: Session, args: dict, user: User | None = None) -> str:
    from src.core import exday_structure as ex

    symbol = str((args or {}).get("symbol") or "").strip().upper()
    if not symbol:
        return "请提供股票代码(symbol)，如 002361。"
    market = str((args or {}).get("market") or "CN").upper()
    if market != "CN":
        return "四档资金结构仅支持 A 股(CN)。"
    try:
        # 单票 RPC 实测 24~26ms, 可直接进请求路径; 顺带写穿 exday_daily(历史只能靠落库累积)
        m = await asyncio.to_thread(ex.fetch, symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_fund_structure 失败 [%s]: %s", symbol, exc)
        return f"四档资金结构获取失败: {str(exc)[:100]}"
    if not m.get("available"):
        return f"[四档资金结构] {m.get('reason', '无数据')}——不做推断。"
    if db is not None:
        try:
            from src.collectors.exday_daily import upsert_rows
            from src.collectors.exday_daily import _today as _ex_today

            row = ex.to_row(m, symbol=symbol.split(".")[0],
                            trade_date=m.get("trade_date") or _ex_today())
            await asyncio.to_thread(upsert_rows, db, [row])
        except Exception as exc:  # noqa: BLE001 - 写穿失败不影响返回
            logger.debug("exday 写穿失败 %s: %s", symbol, exc)
    # 历史分位（有 20 个交易日样本才给，否则明说还需几天）
    hist = _exday_history_pct(db, symbol, "retail_ratio")
    text = ex.render(m, title=symbol)
    if hist.get("available"):
        text += (f"\n历史分位：散户化率 {hist['value']}% 位于近 {hist['days']} 个交易日第 "
                 f"{hist['pct']} 分位")
    elif hist.get("reason"):
        text += f"\n历史分位：{hist['reason']}"
    return text


@register_chat_tool(
    "get_order_granularity",
    schema={
        "type": "function",
        "function": {
            "name": "get_order_granularity",
            "description": (
                "获取个股**委托粒度与拆单结构**（通达信客户端口径）：笔均委托(手)、笔均成交(手)、"
                "委托/成交粒度比(拆单隐匿度)、委托-成交笔数比(一笔委托被拆成几笔成交)、"
                "买/卖撤单率与撤单不对称。回答「有没有拆单」「是不是被动挂单被吃」"
                "「撤单是不是假挂单」「托盘还是压盘」这类微观结构问题时用它。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位代码，如 002361"},
                    "market": {"type": "string", "enum": ["CN"], "description": "默认 CN"},
                },
                "required": ["symbol"],
            },
        },
    },
    caliber="通达信客户端委托/成交笔数口径(tdx_exday4)：拆单为统计代理，非逐笔委托号还原",
)
async def _tool_get_order_granularity(db: Session, args: dict, user: User | None = None) -> str:
    from src.core import exday_structure as ex

    symbol = str((args or {}).get("symbol") or "").strip().upper()
    if not symbol:
        return "请提供股票代码(symbol)，如 002361。"
    market = str((args or {}).get("market") or "CN").upper()
    if market != "CN":
        return "委托粒度仅支持 A 股(CN)。"
    try:
        m = await asyncio.to_thread(ex.fetch, symbol)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_order_granularity 失败 [%s]: %s", symbol, exc)
        return f"委托粒度获取失败: {str(exc)[:100]}"
    if not m.get("available"):
        return f"[委托粒度] {m.get('reason', '无数据')}——不做推断。"
    g = m.get("granularity") or {}
    if not g.get("available"):
        return (f"[委托粒度] {symbol} 缺委托量/笔数（TQ 未返回 BOrder/TotalBOrder 等）"
                "——不推断拆单结构。")
    lines = [
        f"[数据源: 通达信 TQ get_exday_data · 委托/成交笔数口径 · {m.get('trade_date') or '当日'}] {symbol}",
        f"四档阈值：{m.get('tier_thresholds_hint')}",
    ]
    if m.get("quality") != "ok":
        lines.append(f"⚠️ 数据待核：{m.get('reason')}")
    lines += [
        f"笔均委托 {g['avg_order_lots']} 手/笔｜笔均成交 {g.get('avg_trade_lots')} 手/笔"
        f"｜**拆单隐匿度 {g.get('gran_hiding')}×**",
        f"委托/成交笔数比 {g.get('order_fill_ratio')}"
        f"（<1 = 一笔委托被拆成多笔成交；越小被动拆单越重）",
        f"买撤单率 {g.get('cancel_buy_rate')}%｜卖撤单率 {g.get('cancel_sell_rate')}%"
        f"｜撤单不对称 {g.get('cancel_skew')}pp（>0 卖撤更凶=压盘恐吓，<0 买撤更凶=虚假托盘）",
    ]
    tags = [t for t in (m.get("tags") or [])
            if t["code"] in {"passive_split", "granularity_wide", "fake_bid", "fake_ask"}]
    for t in tags:
        lines.append(f"  ▸ {t['label']}：{t['basis']}")
    if not tags:
        lines.append("  未命中拆单/托盘/压盘判据（阈值内）——不硬凑结论。")
    lines.append("⚠️ 口径：拆单结论是**日频笔数比**的统计代理，不等于逐笔委托号还原"
                 "（`.tck` 逆向已证伪被动 maker 委托号落盘）。")
    return "\n".join(lines)


def _exday_history_pct(db: Session | None, symbol: str, field: str, days: int = 20) -> dict:
    """读 exday_daily 算某字段的当日历史分位（样本 <days 就明说还差几天）。"""
    if db is None:
        return {"available": False, "reason": "无 DB 会话，跳过历史分位"}
    try:
        from src.db.models import ExdayDaily

        rows = (
            db.query(ExdayDaily)
            .filter(ExdayDaily.symbol == str(symbol).split(".")[0])
            .order_by(ExdayDaily.trade_date.desc())
            .limit(days)
            .all()
        )
    except Exception as exc:  # noqa: BLE001 - 表未建/查询失败都只是少一段
        return {"available": False, "reason": f"历史分位不可用（{type(exc).__name__}）"}
    vals = [getattr(r, field, None) for r in rows]
    vals = [float(v) for v in vals if v is not None]
    if len(vals) < 10:
        return {"available": False,
                "reason": f"样本不足（exday_daily 仅 {len(vals)} 个交易日，至少 10 天）"}
    cur = vals[0]
    below = sum(1 for v in vals if v < cur)
    return {"available": True, "value": round(cur, 2),
            "pct": round(below / max(len(vals) - 1, 1) * 100, 0), "days": len(vals)}


@register_chat_tool(
    "get_factor_winrate",
    schema={
        "type": "function",
        "function": {
            "name": "get_factor_winrate",
            "description": (
                "读取【量化因子多维胜率与超额收益回测表】（factor_winrate_daily）："
                "包含数智决策 GS 状态与信号（G区/S区/G信号原始 vs 次日确认）、AI 机构活跃度四档、"
                "GS×活跃度共振、威科夫量价四标、涨停封板质量 的 T+1/T+3/T+5 实测胜率、"
                "3%达标率、盈亏比，以及相对同池无条件基准的超额胜率(pp)与超额收益(%)。"
                "回答「GS买卖区间胜率多少」「哪个因子胜率高」「三指标共振实测有效吗」"
                "「放量突破/弹簧试探/首板二板历史胜率」等问题时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "factor_family": {
                        "type": "string",
                        "enum": [
                            "all",
                            "gs_state",
                            "ai_activity",
                            "gs_x_activity",
                            "wyckoff",
                            "limit_up_quality",
                        ],
                        "description": "因子族（默认 all 返回全部五大因子族 + 同池基准）",
                        "default": "all",
                    },
                    "horizon": {
                        "type": "integer",
                        "enum": [1, 3, 5],
                        "description": "持有期交易日（1/3/5，不传则同时返回 T+1/T+3/T+5）",
                    },
                },
                "required": [],
            },
        },
    },
    caliber="factor_winrate_daily 实测回测快照(前复权日线 · 剔除T+1一字涨停不可买 · 附同池无条件基准超额)",
)
async def _tool_get_factor_winrate(db: Session, args: dict, user: User | None = None) -> str:
    from src.core import backtest_lab

    fam = str((args or {}).get("factor_family") or "all").strip().lower()
    raw_h = (args or {}).get("horizon")
    horizon = int(raw_h) if raw_h in (1, 3, 5, "1", "3", "5") else None
    try:
        data = backtest_lab.load_winrates(db, factor_family=fam, horizon=horizon)
        if not data.get("available") and db is not None:
            # 首次调用表为空时触发轻量回测并写穿落库
            backtest_lab.run_and_persist(db, limit_symbols=120, lookback_days=300)
            data = backtest_lab.load_winrates(db, factor_family=fam, horizon=horizon)
        return backtest_lab.render(data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_factor_winrate 失败: %s", exc)
        return f"[因子胜率回测库] 读取异常：{type(exc).__name__}——不做推断。"


@register_chat_tool(
    "get_market_replay",
    schema={
        "type": "function",
        "function": {
            "name": "get_market_replay",
            "description": (
                "读取【盘后复盘全景证据】：涨停家数与连板梯队（首板/2板+/3板+/一字板）、"
                "炸板家数与炸板率、封板率、跌停家数与最长连续跌停、晋级率（昨日涨停今日续板比例）、"
                "涨停行业分布与龙头、情绪周期 6 阶段（冰点/启动/主升/高潮/退潮/修复）、"
                "以及东财与通达信两口径交叉校验差值。"
                "回答「今天涨停几家/连板梯队怎样/炸板率多少/跌停几只/晋级率多高/情绪周期在哪"
                "/主线是什么」等盘后复盘类问题时**优先调用本工具**，"
                "不要再从 get_strategy_signals 里找涨停数据。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "交易日 YYYYMMDD 或 YYYY-MM-DD；不传=最近一个已落库快照",
                    },
                    "refresh": {
                        "type": "boolean",
                        "description": "true=强制重新抓取外部池（盘中/盘后刚收盘时用），默认读库",
                        "default": False,
                    },
                },
                "required": [],
            },
        },
    },
    caliber="东财三池同源同日(ZTPool/ZBPool/DTPool) + TQ池交叉校验; 炸板率=炸板/(涨停+炸板)",
)
async def _tool_get_market_replay(db: Session, args: dict, user: User | None = None) -> str:
    from src.core import market_replay

    date_arg = str((args or {}).get("date") or "").replace("-", "").strip() or None
    refresh = bool((args or {}).get("refresh") or False)
    try:
        replay = None
        if not refresh:
            replay = market_replay.read_latest(db, date_arg)
        if replay is None:
            replay = await asyncio.to_thread(market_replay.build_replay, date_arg)
            if replay:
                await asyncio.to_thread(market_replay.persist, db, replay)
        if not replay:
            return "[盘后复盘] 取数失败：涨停池与跌停池均未返回数据——本维度如实标注无数据，不做推测。"
        return market_replay.render(replay)
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_market_replay 失败: %s", exc)
        return f"[盘后复盘] 读取异常：{type(exc).__name__}——本维度如实标注无数据。"

# ──────────────── 新闻历史检索 + 联网兜底（2026-10-24）────────────────
# 背景（生产实测）：
#  · news_cache 表存在但 **0 行** —— 快讯只拉不存，参谋答不了"上周政策"这类跨时间问题；
#  · 用户明确要求"加实时联网搜索，但只在系统数据源取不到时才启用"。
# 两者互补：search_news 查本地沉淀（免费、可审计），web_search_fallback 才是最后的对外出口。


@register_chat_tool(
    "search_news",
    schema={
        "type": "function",
        "function": {
            "name": "search_news",
            "description": (
                "在【本机已沉淀的新闻库】里按时间窗口+关键词检索历史新闻与政策，"
                "用于回答「上周有哪些政策」「最近三天关于半导体的消息」「这个月证监会说了什么」"
                "这类需要跨时间回溯的问题。"
                "注意：这是查本地历史库（自部署起积累），不是实时抓取；当前快讯请用 get_market_news。"
                "查不到会说清楚覆盖区间，不要据此断言'没有发生'。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "关键词（标题/正文模糊匹配），如「半导体」「降准」；不填则在窗口内按时间倒序返回", "default": ""},
                    "days": {"type": "integer", "description": "回溯天数，默认 7，上限 120", "default": 7},
                    "policy_only": {"type": "boolean", "description": "只返回政策/监管类（工信部/发改委/证监会/央行/国常会/补贴/关税等关键词命中）", "default": False},
                    "limit": {"type": "integer", "description": "返回条数，默认 30，上限 100", "default": 30},
                },
                "required": [],
            },
        },
    },
    caliber="本地沉淀(news_cache, 自部署起积累)",
)
async def _tool_search_news(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.news_store import coverage, search_news

    keyword = (args.get("keyword") or "").strip()
    days = int(args.get("days") or 7)
    policy_only = bool(args.get("policy_only"))
    limit = int(args.get("limit") or 30)
    try:
        rows = await asyncio.to_thread(search_news, db, keyword, days, policy_only, limit)
        cov = coverage(db)
    except Exception as e:  # noqa: BLE001 — 失败如实说，不装成"没有新闻"
        logger.error("search_news 失败: %s", e)
        return f"新闻历史库查询失败：{str(e)[:120]}"

    head = (
        f"【本地新闻库】近 {days} 天"
        f"{'·政策类' if policy_only else ''}"
        f"{'·关键词「' + keyword + '」' if keyword else ''}"
        f"，命中 {len(rows)} 条"
    )
    tail = (
        f"\n库覆盖区间：{cov['from'] or '—'} ~ {cov['to'] or '—'}（共 {cov['rows']} 条，自部署起积累）"
    )
    if not rows:
        return (
            head
            + "\n（本地库该区间无记录 —— 可能是区间早于部署日，或该主题确实没有入库；"
            "不要据此断言事件没发生。需要当下消息请用 get_market_news。）"
            + tail
        )
    lines = [head]
    for r in rows:
        mark = "🏛 " if r.get("policy") else ""
        lines.append(f"- [{r['publish_time']}] {mark}{r['title']} ({r['source']})")
    return "\n".join(lines) + tail


def _tavily_key(db: Session | None) -> str:
    """联网搜索密钥：env 优先，其次 app_settings（便于在设置页配置，不必改容器环境）。"""
    key = (os.environ.get("TAVILY_API_KEY") or "").strip()
    if key or db is None:
        return key
    try:
        from sqlalchemy import text as _text

        row = db.execute(
            _text("SELECT value FROM app_settings WHERE key = 'tavily_api_key' LIMIT 1")
        ).first()
        return (row[0] or "").strip() if row else ""
    except Exception:  # noqa: BLE001
        return ""


def _tavily_search(key: str, query: str, max_results: int = 5) -> list[dict]:
    import json as _json
    import urllib.request

    payload = _json.dumps({
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
    }).encode()
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = _json.loads(resp.read().decode())
    return body.get("results") or []


@register_chat_tool(
    "web_search_fallback",
    schema={
        "type": "function",
        "function": {
            "name": "web_search_fallback",
            "description": (
                "【兜底专用 · 不要主动调用】实时联网搜索（外部引擎）。"
                "**使用条件（必须同时满足）**：① 已经调用过对应的系统数据源工具；"
                "② 该工具返回空、明确不可用（'取不到/数据源不可用'）或明显缺料（如无政策时间线）。"
                "系统数据源有数据时**必须**用系统数据源，不得为了'更全面'而联网。"
                "调用后必须在回答里标注「以下内容来自实时联网搜索，非系统数据源」。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索词，尽量具体（含公司名/政策名/时间）"},
                    "max_results": {"type": "integer", "description": "返回条数，默认 5，上限 10", "default": 5},
                    "reason": {"type": "string", "description": "为什么必须联网：写清哪个系统工具返回了什么空/失败结果。会记入审计日志。", "default": ""},
                },
                "required": ["query"],
            },
        },
    },
    caliber="外部联网搜索(Tavily) — 非系统数据源, 仅兜底",
)
async def _tool_web_search_fallback(db: Session, args: dict, user: User | None = None) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "请提供搜索关键词（query）。"
    n = max(1, min(int(args.get("max_results") or 5), 10))
    reason = (args.get("reason") or "").strip()
    key = _tavily_key(db)
    if not key:
        return (
            "联网搜索未启用：未配置搜索密钥。"
            "请在设置中填写 tavily_api_key（或给容器加 TAVILY_API_KEY 环境变量）。"
            "—— 本条是配置缺失，不代表'搜不到'，请如实告知用户。"
        )
    try:
        rows = await asyncio.to_thread(_tavily_search, key, query, n)
    except Exception as e:  # noqa: BLE001
        logger.warning("联网搜索失败: %s", e)
        return f"联网搜索失败：{str(e)[:120]}（外部引擎不可用，请如实告知用户，不要用其它来源猜测）"
    logger.info("web_search_fallback 触发 query=%r reason=%r", query[:80], reason[:120])
    if not rows:
        return f"联网搜索无结果（query=「{query}」）。"
    lines = [f"【实时联网搜索 · 非系统数据源】query=「{query}」 命中 {len(rows)} 条"]
    if reason:
        lines.append(f"（触发原因：{reason}）")
    for r in rows:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        content = (r.get("content") or "").strip().replace("\n", " ")[:180]
        lines.append(f"- {title}\n  {url}\n  {content}")
    return "\n".join(lines)


# ──────────────── 两融（A股杠杆）────────────────────────────────────────────
# 归因维度「A股杠杆」此前无工具：参谋被问「杠杆资金撤了没」只能绕道猜。
# 数据源悟道 MCP margin_trading（已订阅，SSE/SZSE/BSE 分区，日频，可查区间）。
# 口径：marginBalance=融资余额、totalMarginShortBalance=两融余额，单位**元**（渲染转亿）。


@register_chat_tool(
    "get_margin_trading",
    schema={
        "type": "function",
        "function": {
            "name": "get_margin_trading",
            "description": (
                "查询 A 股融资融券（杠杆资金）余额与变化：交易所汇总(沪/深/北)或单只个股明细。"
                "用于回答「杠杆资金在加还是在撤」「融资余额多少」「两融拥挤度」「去杠杆」等问题。"
                "数据源悟道（日频，T+1 更新）。个股明细传 code。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "可选，6 位股票代码；不传则返回交易所汇总", "default": ""},
                    "startDate": {"type": "string", "description": "区间开始，YYYY-MM-DD 或 YYYYMMDD；不传默认近 20 个交易日", "default": ""},
                    "endDate": {"type": "string", "description": "区间结束，默认今天", "default": ""},
                    "limit": {"type": "integer", "description": "返回条数，默认 40，上限 200", "default": 40},
                },
                "required": [],
            },
        },
    },
    caliber="悟道 margin_trading（交易所披露口径，融资余额/两融余额单位=元）",
)
async def _tool_get_margin_trading(db: Session, args: dict, user: User | None = None) -> str:
    from datetime import datetime, timedelta

    code = (args.get("code") or "").strip()
    end = (args.get("endDate") or "").strip() or datetime.now().strftime("%Y-%m-%d")
    start = (args.get("startDate") or "").strip()
    if not start:
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=35)).strftime("%Y-%m-%d") if "-" in end else (
            datetime.strptime(end, "%Y%m%d") - timedelta(days=35)
        ).strftime("%Y%m%d")
    try:
        limit = max(1, min(int(args.get("limit") or 40), 200))
    except (TypeError, ValueError):
        limit = 40

    payload: dict = {"startDate": start, "endDate": end, "limit": limit,
                     "format": "json", "detailLevel": "raw"}
    if code:
        payload["code"] = code
    try:
        from src.collectors.wudao_mcp_client import WudaoMCPClient

        r = await asyncio.to_thread(lambda: WudaoMCPClient().call_tool("margin_trading", payload))
    except Exception as e:  # noqa: BLE001 — 取不到要如实说，不许编余额
        logger.warning("两融取数失败: %s", e)
        return f"两融数据取数失败：{str(e)[:120]}（不是'没有两融数据'，是数据源不可用）"

    # wudao 返回 {"text": ...} 或直接结构化 dict；两种都要吃得下
    data = r.get("data") if isinstance(r, dict) and isinstance(r.get("data"), dict) else r
    if not isinstance(data, dict):
        txt = (r or {}).get("text") if isinstance(r, dict) else None
        return f"【两融】{txt}" if txt else "两融返回结构无法解析（源返回了非预期格式）。"
    rows = data.get("rows") or []
    if not rows:
        head = (r.get("text") if isinstance(r, dict) else "") or ""
        return f"两融无数据（{start}~{end}）。{('源摘要：' + head) if head else '可能非交易日或数据源未就绪。'}"

    def yi(v):
        try:
            return f"{float(v) / 1e8:.0f}亿"
        except (TypeError, ValueError):
            return "未披露"

    # 按交易日聚合：同一天 SSE/SZSE/BSE 各自一行
    by_date: dict[str, dict] = {}
    for row in rows:
        d = str(row.get("tradeDate") or "")
        if not d:
            continue
        slot = by_date.setdefault(d, {"margin": 0.0, "total": 0.0, "ex": {}})
        m = row.get("marginBalance")
        t = row.get("totalMarginShortBalance")
        if isinstance(m, (int, float)):
            slot["margin"] += m
        if isinstance(t, (int, float)):
            slot["total"] += t
        slot["ex"][str(row.get("exchangeId") or "?")] = (m, t)
    dates = sorted(by_date)
    # ⚠️ 2026-10-25 生产实测的坑：20261023 只返回了 SSE 一家（SZSE 当日未披露），
    # 合并后 12374亿 vs 前一日三家 24269亿 → 区间显示 **-50.94% 的假腰斩**。
    # 交易所披露时点不同步（T+1 各家有先后），**不能把"只到一家"的日子和"三家齐全"的日子
    # 放进同一条序列比较**。修法：按覆盖家数判定"完整日"，区间只取最近两个完整日；
    # 最新日若缺所，单独标注"不可比"，绝不参与涨跌计算。
    cov = {d: len(by_date[d]["ex"]) for d in dates}
    max_cov = max(cov.values())
    complete = [d for d in dates if cov[d] == max_cov]
    latest = dates[-1]
    lm, lt = by_date[latest]["margin"], by_date[latest]["total"]

    lines = [f"【两融（A股杠杆）】{dates[0]} ~ {latest}，共 {len(dates)} 个交易日"]
    per_ex = "；".join(
        f"{k} 融资 {yi(v[0])}" for k, v in sorted(by_date[latest]["ex"].items())
    )
    lines.append(f"- 最新 {latest}：融资余额 {yi(lm)}｜两融余额 {yi(lt)}（{per_ex}）")
    if cov[latest] < max_cov:
        lines.append(
            f"  ⚠️ 该日仅 {cov[latest]}/{max_cov} 家交易所披露（"
            f"{'/'.join(sorted(by_date[latest]['ex']))}），其余当日未出 —— "
            f"**不可与前一日直接比较**，下面区间用最近两个完整交易日计算。"
        )
    if len(complete) >= 2:
        c_last, c_first = complete[-1], complete[0]
        lm_, lt_ = by_date[c_last]["margin"], by_date[c_last]["total"]
        fm, ft = by_date[c_first]["margin"], by_date[c_first]["total"]
        if fm and ft:
            d_m = (lm_ - fm) / fm * 100
            d_t = (lt_ - ft) / ft * 100
            arrow = "增" if lm_ >= fm else "减"
            lines.append(
                f"- 区间变化（{c_first}→{c_last}，均为 {max_cov} 家齐全日）："
                f"融资余额 {yi(fm)} → {yi(lm_)}（{d_m:+.2f}%，{arrow}）｜"
                f"两融余额 {yi(ft)} → {yi(lt_)}（{d_t:+.2f}%）"
            )
    elif len(dates) > 1:
        lines.append("- 区间变化：**无两个完整交易日**，不做涨跌计算（缺所数据不可比）。")
    # 只给最近 5 个交易日的日序列，缺所的日子标 *，避免把不完整的数当完整数看
    seq = "；".join(
        f"{d} {yi(by_date[d]['margin'])}" + ("*" if cov[d] < max_cov else "")
        for d in dates[-5:]
    )
    lines.append(f"- 近 5 日融资余额：{seq}（* = 当日缺所披露，不可比）")
    lines.append(
        "口径：融资余额/两融余额均取交易所披露值，单位元已换算为亿；"
        "个股明细请传 code。T+1 更新，不含当日盘中。"
    )
    return "\n".join(lines)


# ──────────────── 宏观序列（外部环境）──────────────────────────────────────
# 归因维度「美债/油价/美元/风险偏好」此前完全没有数据来源，参谋只能编。
# 后端无 iFinD 客户端（容器内只有 THS_*），故走 Yahoo chart API：免 key、日频、有历史。
# 口径：^TNX 自适应归一（>20 视为 ×10 旧格式）；缺失的指标如实列 reason，不插值不补 0。


@register_chat_tool(
    "get_macro_series",
    schema={
        "type": "function",
        "function": {
            "name": "get_macro_series",
            "description": (
                "查外部宏观/风险偏好指标的历史序列与区间涨跌：美债10Y收益率、布伦特/WTI原油、"
                "美元指数、VIX、标普500、COMEX黄金。用于回答「美债升了没」「油价对A股的影响」"
                "「外部环境紧不紧」等归因问题。数据源 Yahoo（日频，免 key，非交易所官方口径）。"
                "注意：这是**外部市场口径**，不是国内宏观（GDP/CPI/社融没有），GDP类请如实说没有。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "指标列表：us10y/brent/wti/dxy/vix/spx/gold/us2y；不传默认 美债+油价+美元指数",
                    },
                    "range": {"type": "string", "description": "回溯区间：1mo/3mo/6mo/1y/2y，默认 3mo", "default": "3mo"},
                },
                "required": [],
            },
        },
    },
    caliber="Yahoo Finance（外部市场口径，日频；非国内宏观、非交易所官方）",
)
async def _tool_get_macro_series(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.macro_free_sources import SERIES, summarize

    keys = args.get("keys") or ["us10y", "brent", "dxy"]
    if isinstance(keys, str):
        keys = [k.strip() for k in keys.split(",") if k.strip()]
    range_ = str(args.get("range") or "3mo").strip()
    if range_ not in ("1mo", "3mo", "6mo", "1y", "2y"):
        range_ = "3mo"

    def _work():
        return [summarize(k, range_) for k in keys]

    try:
        rows = await asyncio.to_thread(_work)
    except Exception as e:  # noqa: BLE001
        logger.warning("宏观序列取数失败: %s", e)
        return f"宏观序列取数失败：{str(e)[:100]}（外部源不可用，不是'没有数据'）"

    ok = [r for r in rows if r.get("points")]
    bad = [r for r in rows if not r.get("points")]
    lines = [f"【外部宏观（{range_}）】口径=Yahoo 日频，非国内宏观数据"]
    for r in ok:
        lines.append(
            f"- {r['name']}：{r['first_date']} {r['first']}{r['unit']} → "
            f"{r['last_date']} {r['last']}{r['unit']}（{r['change_pct']:+.2f}%，"
            f"区间高 {r['high']}{r['unit']} / 低 {r['low']}{r['unit']}）"
        )
    for r in bad:
        lines.append(f"- {r.get('key')}：未取到（{r.get('reason') or '源无返回'}）")
    if len(ok) == 1:
        lines.append(
            "⚠️ 只有一个指标有数据，不能据此推出'大盘为什么跌'——因果需要多指标同向 + 国内资金面配合。"
        )
    lines.append(
        "用法纪律：宏观指标只能说明'外部环境是否支持下跌'，**不能单独当因果**；"
        "要和两融/资金流/涨跌家数同向才构成证据链。"
    )
    return "\n".join(lines)


# ──────────────── 任意区间走势（区间问答的唯一正解）──────────────────────────
# 2026-10-25：用户问「上证 7月到10月为什么跌」，模型只能用 get_trend_profile 的
# 固定窗口（5/20/60/120 日），于是答成「60日 +5.31%，没有趋势下跌」——
# 而实测 7 月 4112 → 10 月 3658 是 -12%。固定窗口回答不了任意区间，必须给专用工具。


@register_chat_tool(
    "get_range_summary",
    schema={
        "type": "function",
        "function": {
            "name": "get_range_summary",
            "description": (
                "【任意区间走势】给定起止日期，算某个标的在这段区间的真实涨跌、最高最低、"
                "成交量变化、期间最大回撤。**支持指数**（000001/399001/399006/000300 等），"
                "指数读 index_daily，个股读 klines 前复权。"
                "凡是问题里带具体时间段（「7月到10月」「上个月」「今年以来」）就必须用它，"
                "不要用 get_trend_profile 的固定窗口（那只有 5/20/60/120 日）代替。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "代码：指数如 000001，个股如 600111"},
                    "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                    "market": {"type": "string", "description": "CN/HK/US，默认 CN", "default": "CN"},
                },
                "required": ["symbol", "start_date", "end_date"],
            },
        },
    },
    caliber="指数=index_daily / 个股=klines 前复权（区间口径，非年化）",
)
async def _tool_get_range_summary(db: Session, args: dict, user: User | None = None) -> str:
    from src.core.trend_profile import build_range_summary

    symbol = str(args.get("symbol") or "").strip().upper()
    start = str(args.get("start_date") or "").strip()
    end = str(args.get("end_date") or "").strip()
    market = str(args.get("market") or "CN").strip().upper() or "CN"
    if not symbol or not start or not end:
        return "需要 symbol + start_date + end_date 三个参数（日期格式 YYYY-MM-DD）。"
    try:
        return await asyncio.to_thread(build_range_summary, symbol, start, end, market)
    except Exception as exc:  # noqa: BLE001
        return f"[区间走势] 取数失败：{type(exc).__name__}——不做推断。"

# ──────────────── 市场级排行（腾讯免配额源）─────────────────────────────
# 2026-10-25：通达信问小达配额用尽（MCP error -32603），「今日主力净流入前10」
# 「今日涨幅前10的概念板块」这类市场级排行开始答不出来。腾讯排行 API 免费、实时、
# 个股+行业+概念+地区全覆盖，字段含涨跌幅/换手/量比/主力净流入/板块涨跌家数/领涨股。


@register_chat_tool(
    "get_market_ranking",
    schema={
        "type": "function",
        "function": {
            "name": "get_market_ranking",
            "description": (
                "【市场级排行】按维度取实时榜单，覆盖个股/行业板块/概念板块/地区板块。"
                "metric 可选：change(涨跌幅) / amount(成交额) / speed(涨速) / main_inflow(主力净流入)。"
                "典型问法：「今日主力净流入前10的A股」「今日涨幅前10的概念板块」「哪个行业最强」。"
                "板块结果里的涨跌家数是**上涨/下跌家数**，不是涨停家数——问涨停家数要另用涨停池。"
                "数据为腾讯排行实时快照、非复权。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["stock", "industry", "concept", "region"],
                        "description": "个股 / 行业板块 / 概念板块 / 地区板块，默认 stock",
                    },
                    "metric": {
                        "type": "string",
                        "enum": ["change", "amount", "speed", "main_inflow"],
                        "description": "排序指标，默认 change",
                    },
                    "limit": {"type": "integer", "description": "返回条数 1-50，默认 10"},
                },
                "required": [],
            },
        },
    },
    caliber="腾讯排行 · 实时快照 · 非复权；板块涨跌家数≠涨停家数",
)
async def _tool_get_market_ranking(db: Session, args: dict, user: User | None = None) -> str:
    from marketdata.vendors.tencent_rank import METRIC_LABEL, SCOPE_LABEL, fetch_board_rank

    scope = str(args.get("scope") or "stock").strip().lower()
    metric = str(args.get("metric") or "change").strip().lower()
    try:
        limit = int(args.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 50))  # 边界防御：不把模型给的 999 原样传给上游

    res = await asyncio.to_thread(fetch_board_rank, scope, metric, limit)
    if not res.get("available"):
        return f"[市场排行] 取不到数据：{res.get('reason') or '未知原因'}——不做推断，也不当作“今天没有”。"

    rows = res["rows"]
    head = (
        f"[{SCOPE_LABEL.get(scope, scope)} · {METRIC_LABEL.get(metric, metric)}榜] "
        f"共 {len(rows)} 条 | 口径：{res['caliber']}"
    )
    lines = [head]
    for i, r in enumerate(rows, 1):
        chg = r.get("change_pct")
        chg_s = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "—"
        if scope == "stock":
            mi = r.get("main_inflow")
            mi_s = f"{mi / 10000:+.2f}亿" if isinstance(mi, (int, float)) else "—"
            lines.append(
                f"{i}. {r.get('name')}({r.get('code')}) 现价 {r.get('price')} 涨跌 {chg_s} "
                f"换手 {r.get('turnover_rate')}% 量比 {r.get('volume_ratio')} 主力净流入 {mi_s}"
            )
        else:
            mi = r.get("main_inflow")
            mi_s = f"{mi / 10000:+.2f}亿" if isinstance(mi, (int, float)) else "—"
            leader = r.get("leader") or {}
            ld = leader.get("name") or "—"
            ldp = leader.get("change_pct")
            ld_s = f"{ldp:+.2f}%" if isinstance(ldp, (int, float)) else "—"
            lines.append(
                f"{i}. {r.get('name')}({r.get('code')}) 涨跌 {chg_s} 主力净流入 {mi_s} "
                f"涨/跌家数 {r.get('up_down') or '—'} 领涨 {ld} {ld_s} 换手 {r.get('turnover_rate')}%"
            )
    if scope != "stock":
        lines.append("注：涨/跌家数是板块内上涨与下跌家数，不是涨停家数。")
    return "\n".join(lines)



# ── 注册顺序修正（2026-10-26）─────────────────────────────────────────────
# `get_stock_report` 当初被插在「核心段」与「thsdk 段」之间，破坏了本仓
# **"新工具一律追加到注册表末尾"** 的纪律，直接后果是 `chat_tool_schemas()` 的
# 索引钉子整体错位（`names[38]` 本该是 thsdk 段，变成了 get_stock_report）。
#
# 这里不动函数定义（挪代码风险大、diff 也难审），只在注册完成后**重排**尾部：
#   … → get_stock_report → get_market_ranking（最新）
# 结果：38 恢复为 thsdk 段，末位仍是最新工具；两条既有钉子同时成立。
_REORDER_TAIL = ("get_stock_report", "get_market_ranking")
_moved = {name: CHAT_TOOL_REGISTRY.pop(name) for name in _REORDER_TAIL if name in CHAT_TOOL_REGISTRY}
for _name in _REORDER_TAIL:
    if _name in _moved:
        CHAT_TOOL_REGISTRY[_name] = _moved[_name]
del _moved, _name
