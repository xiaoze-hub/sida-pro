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


CHAT_TOOL_REGISTRY: dict[str, ChatTool] = {}


def register_chat_tool(
    name: str,
    *,
    schema: dict | None = None,
    caliber: str,
    requires: str = "",
):
    """注册对话工具(schema/handler/caliber/requires 同处一处), 重复注册报错。"""

    def _deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        if name in CHAT_TOOL_REGISTRY:
            raise ValueError(f"chat 工具重复注册: {name}")
        CHAT_TOOL_REGISTRY[name] = ChatTool(
            name=name, schema=schema, handler=fn, caliber=caliber, requires=requires,
        )
        return fn

    return _deco


def chat_tool_schemas() -> list[dict]:
    """按注册顺序导出全部 function schema(handler-only 工具不导出)。"""
    return [t.schema for t in CHAT_TOOL_REGISTRY.values() if t.schema is not None]


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


@register_chat_tool(
    "tdx_wenda",
    schema={
        "type": "function",
        "function": {
            "name": "tdx_wenda",
            "description": "通达信问小达：自然语言投研查询。用于回答市场级的选股/排行/资金流向问题，如“今日主力净流入前10的A股”“今日涨幅前10的概念板块”“近3日主力净流入前10的半导体”“今日涨停家数最多的概念板块”“今日龙虎榜机构净买入前10”。适合不指定个股、而是看板块/全市场维度的问题。",
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
            return f"通达信问小达未返回数据: {question}"
        rows = res.get("data") or []
        if not rows:
            return f"通达信问小达无结果: {question}"
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
                "kind=annotation：在K线图上加一条标注，需要 symbol + label，可选 price(水平线)/ts(时间点)/note(依据)；"
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
                    "price": {"type": "number", "description": "标注价位（水平线）"},
                    "ts": {"type": "string", "description": "标注时间点，如 2026-10-20 或完整 ISO"},
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
                "回答「这票强弱在什么水平 / 波动大不大 / 是不是放量」时用它，",
            )
            ,
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
    return _build_factors(db, symbol, "CN")


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
    from src.core.scenario_engine import build as _build_scenarios

    symbol = str(args.get("symbol") or "").strip().upper()
    if not symbol:
        return "请提供股票代码(symbol)，如 002050。"
    import asyncio as _asyncio

    try:
        return await _asyncio.to_thread(_build_scenarios, db, symbol, "CN")
    except Exception as exc:  # noqa: BLE001
        return f"[情景矩阵] 取数失败：{type(exc).__name__}——不硬编情景。"


# ──────────────── 数据域目录工具(2026-10-21, 数据域架构 P0) ────────────────
# 位置: 追加在导出清单**最末**。core 工具是连续前缀(EXPECTED_CORE_ORDER index 断言),
# 新增域工具不得插到 core 段中间; 排在尾部既不打乱既有顺序, 也便于比对工具总数。
# 实现收口在 tools_catalog.py(注册顺序 = 本模块 register_catalog_tools 内的顺序)。
from src.agents.chat import tools_catalog as _tools_catalog  # noqa: E402  (仅 stdlib 级依赖)

_tools_catalog.register_catalog_tools()

