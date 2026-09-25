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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

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
    from src.web.api.chat import _fetch_realtime_context

    symbol = args.get("symbol", "")
    market = args.get("market", "CN")
    result = await _fetch_realtime_context(symbol, market)
    return result or f"未能获取 {market}:{symbol} 的行情数据。"


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
            "description": "获取A股集合竞价数据（9:25后当日竞价已生成）：竞价全景（竞价涨停/跌停/涨停委买额/成交额/昨炸板反馈/昨涨停反馈）、竞价最强个股（按bidStrength/金额/涨幅）、竞价主线题材、弱转强/被核风险。用于回答「集合竞价怎么样」「今天竞价最强是谁」「竞价主线是什么」「竞价有超预期的吗」「昨高标被核了吗」「今天弱转强的是谁」等竞价相关问题。注意：9:25前当日竞价数据未生成，会明确提示。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {"type": "string", "description": "竞价场景：overview(竞价全景，默认)/strongest(最强个股)/theme(主线题材)/weak_to_strong(弱转强)/risk(被核风险)/watchlist(盯盘名单)"},
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
    return await _fetch_auction_context(scene, limit)


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

    return await _read_sentiment_cycle()


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
    try:
        from src.core.factor_ic_report import generate_factor_ic_report

        result = await asyncio.to_thread(generate_factor_ic_report, market, None)
        if not result:
            return f"[数据源: 因子IC/IR评估] {market} 因子样本不足或 AI 归因失败(静默降级), 无法生成报告。"
        lines = [
            f"[数据源: 因子IC/IR评估 + AI归因] {market} 因子有效性归因:",
            f"  总评: {result.get('summary')}",
        ]
        for fa in result.get("factor_assessment") or []:
            lines.append(f"  - {fa.get('factor_code')}: {fa.get('assessment')} — {fa.get('note')}")
        if result.get("adjustment_suggestion"):
            lines.append(f"  调权建议: {result['adjustment_suggestion']}")
        lines.append(f"  置信度: {result.get('confidence')}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"get_factor_ic_report 工具失败 [{market}]: {e}")
        return f"因子 IC 归因报告失败: {str(e)[:100]}"


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
    ok, data = _tq_vendor_call("ipo_info", ipo_type, ipo_date)
    if not ok:
        return _tq_unavailable(str(data))
    scope = "今天" if only_today else "今天及以后"
    what = {0: "新股", 1: "新债", 2: "新股/新债"}[ipo_type]
    if not isinstance(data, list) or not data:
        return f"通达信申购日历({scope})里没有{what}。(数据源: 通达信客户端 get_ipo_info)"
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
            return f"🔗 可转债条款(通达信, {c}): {body or '(客户端未返回字段)'}"
    if last_err:
        # 全部候选都失败才是真失败 —— 只在这里打一条, 日志与最终结论一致。
        logger.warning("chat TQ 工具 kzz_info 全部候选失败(%s): %s", "/".join(cands), last_err)
        return _tq_unavailable(last_err)
    return f"通达信里查不到可转债 {raw}(试过 {'/'.join(cands)}) —— 代码是否正确或已退市?"


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
    ok, v = _tq_vendor_call("relation", code)
    if not ok:
        return _tq_unavailable(str(v))
    if not isinstance(v, list):
        return f"通达信没给出 {code} 的板块归属(可能非 A 股代码)。"
    rows = [r for r in v if isinstance(r, dict)]
    if not rows:
        return f"通达信没给出 {code} 的板块归属(可能非 A 股代码)。"
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
    ok, v = _tq_vendor_call("match_stkinfo", kw)
    if not ok:
        return _tq_unavailable(str(v))
    rows = [r for r in (v or []) if isinstance(r, dict) and r.get("Code")]
    if not rows:
        return f"通达信里没有匹配「{kw}」的证券(名称/代码是否正确?)。"
    lines = [f"🔍 通达信检索「{kw}」(共 {len(rows)} 条):"]
    for r in rows[:15]:
        code = str(r.get("Code") or "")
        mkt = _tq_market_cn(code)
        lines.append(f"· {r.get('Name') or '?'} {code}" + (f"({mkt})" if mkt else ""))
    if len(rows) > 15:
        lines.append(f"(仅显示前 15 条, 共 {len(rows)} 条)")
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
                f"指数代码或后缀是否正确?")

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
