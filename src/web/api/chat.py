"""AI 对话 API 端点。"""

import asyncio
import html.parser
import json
import logging
import os
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.config import Settings
from src.core.ai_client import AIClient
from src.web.api.auth import get_current_user
from src.web.database import SessionLocal, get_db
from src.web.models import (
    AIModel,
    AIService,
    AnalysisHistory,
    ChatConversation,
    ChatMessage,
    EntryCandidate,
    Notification,
    PaperTradingPosition,
    Position,
    Stock,
    StockSuggestion,
    StrategySignalRun,
    User,
)

logger = logging.getLogger(__name__)
router = APIRouter()

SYSTEM_PROMPT = """你是数智分析BOT,是 SIDA(Stock-Intelligent-Data-Analytics 数智分析)的 AI 投资助手。

你可以使用工具获取用户的投资数据。当用户的问题涉及具体数据时，主动调用工具获取，不要让用户自己提供。

规则：
- 需要数据时主动调用工具，不要反问用户要数据
- 基于工具返回的实时数据回答，不编造价格等具体数据
- 给出明确的观点和理由
- 涉及买卖建议时说明风险
- 合规声明(2026-08-14): 回答末尾如需给买卖倾向/预测结论, 必须附带「以上分析仅供参考, 不构成投资建议」; 严禁承诺收益或保证盈利
- 用中文回答
- 保持简洁，避免冗余
- 用户问「新闻 / 资讯 / 热点 / 今天有什么消息」类问题时，必须调用 get_market_news 工具获取实时资讯热榜与每日简报，再基于返回内容回答；严禁在不调用工具的情况下凭记忆编造新闻、题材或资金流向。若工具返回为空，如实说明「暂无实时资讯数据」并建议盘后重试。
- 工具选择指引(2026-08-11): 用户问「主力意图/主力在吸筹还是派发/主力想干什么」时, 必须调用 get_main_intent(逐笔口径,含筹码/参与度);「资金流向/主力净流入多少/超大单大单」时调用 get_capital_flow(东财四档口径)。两工具口径不同, 主力意图判断一律以 get_main_intent 为准, get_capital_flow 仅作资金面参考; 若两者方向冲突, 说明口径差异(逐笔vs东财)并优先采信 get_main_intent。严禁用 get_capital_flow 的数据直接下「主力派发/吸筹」结论。
- 决策先锋三指标(2026-08-30): 用户问「决策先锋/三指标共振/GS策略/G买G卖/机构活跃度/AI机构活跃度/暗盘资金」时, 调用 get_decision_pioneer(机构活跃度+GS策略+L2主力净流入三合一)。「主力意图/吸筹派发」仍走 get_main_intent; 问「L2主力净流入」用 get_decision_pioneer 的 L2 字段(TQ口径), 问「东财四档资金流向」用 get_capital_flow。三者口径不同, 数字冲突时须说明口径差异, 不可混用下结论。
- 口径标注规则(2026-08-13): 工具返回文本开头自带数据源口径标注(get_main_intent 为「腾讯逐笔·主力意图口径」, get_capital_flow 为「东财四档·资金流向口径」)。回答涉及「主力净流入/净流出」等具体数字时, 必须说明所用口径(逐笔 or 东财四档), 不得省略; 若两个口径数字不同, 要指出差异原因(统计方式不同: 逐笔主动买卖盘 vs 按大中小单四档归类), 再给结论。
- thsdk 数据源指引(2026-08-20): thsdk 数据源包含 19 个同花顺独有接口, 游客账户可用 15 个(主力净流入/指数/港股返 0)。用户问个股新闻/公司行动/DDE/沪深300/可转债/基金/增强版问财时, 优先用 thsdk 工具(get_thsdk_news/get_thsdk_corporate_action/get_thsdk_dde/get_thsdk_hs300_constituents/get_thsdk_market_data_bond/get_thsdk_market_data_fund/get_wencai_enhanced 等)。thsdk 数据源不可用(工具返回 available=false 或提示数据源不可用)时, 如实告知并回退到其他数据源(东财/腾讯/通达信)。
- 网页链接处理(2026-08-14): 用户发送网页链接(如 mp.weixin.qq.com 微信公众号文章、新闻/研报网页)或要求分析某链接内容时, 必须先调用 get_web_content 工具抓取链接正文, 再基于抓取内容回答; 严禁不抓取就凭空猜测或编造链接内容。若抓取失败(链接非法/超时/非网页/网络错误), 如实告知用户无法获取链接内容及原因, 不得伪造抓取结果。"""

MAX_HISTORY_MESSAGES = 20
MAX_TOOL_ROUNDS = 5

# 工具名 → 流式阶段提示文案(tool 执行前推送给前端, 消除长等待白屏)
_TOOL_STAGE_LABELS = {
    "get_portfolio": "正在读取您的持仓...",
    "get_stock_quote": "正在查询实时行情...",
    "get_technical_analysis": "正在获取技术面分析...",
    "get_main_intent": "正在分析主力意图(逐笔口径)...",
    "get_decision_pioneer": "正在分析决策先锋三指标(GS/暗盘/机构活跃度)...",
    "get_rally_analysis": "正在分析盘中拉升段...",
    "get_stock_suggestions": "正在读取历史建议...",
    "get_watchlist": "正在读取自选股...",
    "get_capital_flow": "正在查询主力资金流向...",
    "get_web_content": "正在抓取网页链接内容...",
    "tdx_wenda": "正在查询市场数据...",
    "get_market_news": "正在获取市场资讯...",
    "get_kline_patterns": "正在识别K线形态...",
    "get_auction_data": "正在获取集合竞价数据...",
    "get_forecast": "正在读取系统预测...",
    "get_opportunities": "正在读取今日机会候选...",
    "get_sentiment_cycle": "正在判别短线情绪周期...",
    "get_strategy_signals": "正在读取策略信号...",
    "get_notifications": "正在读取系统通知...",
    "get_fundamentals_detail": "正在查询基本面明细(龙虎榜/股东/分红/两融/事件)...",
    "get_irm_qa": "正在查询互动易问答(巨潮官方回应)...",
    "get_market_anomalies": "正在获取异动股池(东财)...",
    "get_northbound": "正在查询北向资金(同花顺口径)...",
    "get_hot_stocks": "正在获取同花顺热榜...",
    "get_thsdk_news": "正在查询同花顺个股新闻...",
    "get_thsdk_corporate_action": "正在查询公司行动(分红/送转)...",
    "get_thsdk_dde": "正在查询 DDE 大单动向...",
    "get_thsdk_hs300_constituents": "正在获取沪深300成分股...",
    "get_thsdk_market_data_cn_extended": "正在查询 A 股扩展行情(主力净流入)...",
    "get_thsdk_market_data_index": "正在查询指数实时行情...",
    "get_thsdk_market_data_hk": "正在查询港股实时行情...",
    "get_thsdk_market_data_us": "正在查询美股实时行情...",
    "get_thsdk_market_data_bond": "正在查询可转债行情...",
    "get_thsdk_market_data_fund": "正在查询基金/ETF行情...",
    "get_wencai_enhanced": "正在执行增强版问财检索...",
    "get_main_flow_compare": "正在比对主力双源(腾讯逐笔/同花顺L2)...",
    "get_delta_series": "正在计算秒级Delta序列(逐笔穿透)...",
    "get_orderbook": "正在采集盘口演变快照(THS L2 20档)...",
    "get_event_catalyst": "正在推理事件催化与预期差(公告→受益链)...",
    "get_intent_explain": "正在解读主力意图(规则结论+AI解释)...",
    "get_factor_ic_report": "正在生成因子IC归因报告...",
}

# 画像注入节流: profile_text 截断 + rules 只取前 N 条, 避免每次对话占过多 token
_SHADOW_PROFILE_TEXT_MAX = 300
_SHADOW_PROFILE_RULES_MAX = 3


def _build_shadow_profile_block(profile_json) -> str:
    """从 users.shadow_profile_json 构建精简版画像注入文本(无画像返回空串)。"""
    if not profile_json or not isinstance(profile_json, dict):
        return ""
    parts: list[str] = []

    profile_text = (profile_json.get("profile_text") or "").strip()
    if profile_text:
        if len(profile_text) > _SHADOW_PROFILE_TEXT_MAX:
            profile_text = profile_text[:_SHADOW_PROFILE_TEXT_MAX] + "…"
        parts.append(f"画像: {profile_text}")

    rules = profile_json.get("rules") or []
    if rules:
        rule_lines = []
        for rule in rules[:_SHADOW_PROFILE_RULES_MAX]:
            if isinstance(rule, dict) and rule.get("human_text"):
                rule_lines.append(f"- {rule['human_text']}")
        if rule_lines:
            parts.append("交易规则:\n" + "\n".join(rule_lines))

    preferred_markets = profile_json.get("preferred_markets") or []
    if preferred_markets:
        parts.append("偏好市场: " + ", ".join(str(m) for m in preferred_markets))

    holding_days = profile_json.get("typical_holding_days")
    if holding_days:
        if isinstance(holding_days, (list, tuple)) and len(holding_days) == 2:
            parts.append(f"典型持仓天数: 中位 {holding_days[0]} 天 / P75 {holding_days[1]} 天")
        else:
            parts.append(f"典型持仓天数: {holding_days} 天")

    if not parts:
        return ""
    return "以下是用户交易风格画像(AI 参考, 用于给出更贴合的建议):\n" + "\n".join(parts)

# ──────────────── Tool Definitions ────────────────

CHAT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_portfolio",
            "description": "获取用户的实盘持仓和模拟盘持仓。用于回答持仓相关问题（持仓健康吗、该调仓吗、盈亏情况等）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
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
    {
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
    {
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
    {
        "type": "function",
        "function": {
            "name": "get_decision_pioneer",
            "description": "获取股票决策先锋三指标（GS策略趋势 + 暗盘资金/L2主力净流入 + AI机构活跃度）：机构活跃度数值与档位(生命线1.56/强势线3/大牛线6)、连强天数、5日均值，GS策略G买/S卖信号与当前G区/S区状态，L2主力净流入(TQ口径,对齐同花顺暗盘)。回答'三指标共振''机构活跃度''GS策略信号''暗盘资金''决策先锋'等问题。",
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
    {
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
    {
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
    {
        "type": "function",
        "function": {
            "name": "get_watchlist",
            "description": "获取用户的自选股（关注列表）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
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
    {
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
    {
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
    {
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
    {
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
    {
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
    {
        "type": "function",
        "function": {
            "name": "get_sentiment_cycle",
            "description": "读取当前 A 股短线情绪周期(冰点/修复/发酵/高潮/退潮)及操作提示。用于回答「现在市场情绪怎么样」「短线情绪处于什么阶段」「现在适合打板还是防守」等问题。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
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
    {
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
    {
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
    {
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
    {
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
    {
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
    {
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
    {
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
    {
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
    {
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
    {
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
    {
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
    {
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
    {
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
]

def _build_thsdk_chat_schemas() -> list:
    """构建 thsdk 工具的 CHAT_TOOLS function schema 列表(延迟 import 避免循环依赖)。"""
    from src.agents.chat.tools_thsdk import build_thsdk_tool_schemas
    return build_thsdk_tool_schemas()


# thsdk 工具名 → 集合(供 _execute_tool 路由判断)
_THSDK_TOOL_NAMES_SET: frozenset = frozenset(
    {
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
    }
)


async def _exec_thsdk_tool(name: str, args: dict) -> str:
    """执行 thsdk 工具(选项 C, 2026-08-20)。

    通过 tools_thsdk 间接调用 thsdk_l2, 工具函数内部已做降级(available=false),
    不会抛异常到用户层。同步网络调用包 to_thread 防阻塞事件循环。
    """
    import asyncio

    from src.agents.chat.tools_thsdk import (
        THSDK_TOOL_HANDLERS,
        format_thsdk_tool_result,
    )

    handler = THSDK_TOOL_HANDLERS.get(name)
    if handler is None:
        # 防御: 若 handler 未注册(新工具未同步 registry), 降级返回
        return f"[thsdk] 工具 {name} 尚未注册实现。"
    try:
        result = await asyncio.to_thread(handler, **_filter_tool_args(handler, args))
        return format_thsdk_tool_result(name, result)
    except Exception as e:  # noqa: BLE001
        logger.warning("thsdk 工具执行失败 %s: %s", name, e)
        return f"[thsdk] {name} 执行出错: {str(e)[:80]}"


def _filter_tool_args(handler, args: dict) -> dict:
    """按 handler 的可选参数过滤 args, 只传显式提供的参数, 避免 schema 与实现不一致。"""
    import inspect

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


# 注册 thsdk 11 个高价值工具到对话助手(2026-08-20,选项 C)
CHAT_TOOLS.extend(_build_thsdk_chat_schemas())


def _build_watchlist_context(db: Session, user: User | None = None) -> str:
    """构建用户自选股列表。

    S5(2026-08-26): 传入 user 时只返回本人自选 + user_id=NULL 全局自选;
    不传保持旧行为(内部工具兼容)。
    """
    query = db.query(Stock).order_by(Stock.sort_order.asc())
    if user is not None:
        query = query.filter(or_(Stock.user_id == user.id, Stock.user_id.is_(None)))
    stocks = query.all()
    if not stocks:
        return "用户暂无自选股。"
    lines = [f"- {s.name}({s.market}:{s.symbol})" for s in stocks]
    return "自选股列表：\n" + "\n".join(lines)


# ──────────────── 系统数据工具(2026-08-13): 预测/机会/策略信号/通知 ────────────────

_FORECAST_DB_PATH = os.path.join(os.path.expanduser("~"), ".panwatch_forecast.db")

# 预测方向英文 → 中文
_FORECAST_DIRECTION_CN = {"up": "看涨", "down": "看跌", "sideways": "横盘", "neutral": "中性"}

# forecasts 表(展示层) 与 prediction_runs 表(运行层, final_* 前缀) 的列映射,
# 两表均可能因部署形态存在, 读取时按实际表结构自适应
_FORECAST_COLUMN_MAP = {
    "forecasts": {
        "symbol": "symbol", "stock_name": "stock_name", "last_close": "last_close",
        "direction": "direction", "expected_pct": "expected_pct",
        "confidence": "confidence", "target_price": "target_price",
        "target_date": "target_date", "created_at": "created_at",
    },
    "prediction_runs": {
        "symbol": "symbol", "stock_name": "stock_name", "last_close": "last_close",
        "direction": "final_direction", "expected_pct": "final_expected_pct",
        "confidence": None, "target_price": "final_target_price",
        "target_date": "target_date", "created_at": "created_at",
    },
}


def _resolve_forecast_db_path() -> str:
    """解析预测引擎 SQLite 路径(与 forecast_lib.forecast_paths 同源: 环境变量优先, 默认 ~/.panwatch_forecast.db)。"""
    configured = os.getenv("FORECAST_DB_PATH", "")
    return os.path.abspath(os.path.expanduser(configured or _FORECAST_DB_PATH))


def _read_forecast(symbol: str = "", limit: int = 5) -> str:
    """读取系统最近预测(预测引擎独立库, 只读; 有 outcome 对照时优先展示, 无则返回预测本身)。"""
    db_path = _resolve_forecast_db_path()
    if not os.path.exists(db_path):
        return "暂无系统预测（未找到预测引擎数据库，预测引擎可能尚未运行）。"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            cur = conn.cursor()
            tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            table = "forecasts" if "forecasts" in tables else ("prediction_runs" if "prediction_runs" in tables else None)
            if not table:
                return "暂无系统预测（预测引擎数据库中无预测表）。"
            cmap = _FORECAST_COLUMN_MAP[table]
            base_cols = ("symbol", "stock_name", "last_close", "direction", "expected_pct", "target_price", "target_date", "created_at")
            cols = [cmap[c] for c in base_cols if cmap.get(c)]
            if cmap.get("confidence"):
                cols.append(cmap["confidence"])
            sql = f"SELECT {', '.join(cols)} FROM {table}"
            where, params = "", []
            if symbol:
                where, params = " WHERE symbol = ?", [symbol]
            sql += where + " ORDER BY created_at DESC LIMIT ?"
            params.append(str(max(1, min(int(limit), 50))))
            rows = cur.execute(sql, params).fetchall()
            if not rows:
                return "暂无系统预测" + (f"（{symbol}）" if symbol else "") + "。"
            today = datetime.now().date().isoformat()
            lines = [f"【系统预测】最近{len(rows)}条" + (f"（{symbol}）" if symbol else "") + f"，来自预测引擎 {table} 表。",
                     "⚠️ 警告：历史回测准确率仅31.7%，预测方向不可靠，仅供参考，不可作为交易依据。"]
            for r in rows:
                # 列名统一回写为规范名(final_direction → direction 等), 便于下方格式化
                key_map = {actual: canon for canon, actual in cmap.items() if actual}
                d = {key_map.get(k, k): v for k, v in zip(cols, r)}
                direction = (d.get("direction") or "").strip()
                dir_cn = _FORECAST_DIRECTION_CN.get(direction.lower(), direction or "未知")
                pct = d.get("expected_pct")
                pct_str = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else (str(pct) if pct else "")
                target = d.get("target_price")
                target_str = f"{target:.2f}" if isinstance(target, (int, float)) else (str(target) if target else "—")
                close = d.get("last_close")
                close_str = f"{close:.2f}" if isinstance(close, (int, float)) else (str(close) if close else "—")
                tdate = (d.get("target_date") or "")[:10]
                expired = "已到期" if (tdate and tdate < today) else ("未到期" if tdate else "—")
                created = (d.get("created_at") or "")[:16]
                conf = d.get("confidence") if cmap.get("confidence") else None
                conf_str = f" 置信度:{conf}" if conf else ""
                line = (f"- {d.get('symbol')} {d.get('stock_name') or ''} {dir_cn} "
                        f"预期{pct_str} 目标价{target_str} 现价{close_str}"
                        f"{conf_str} 到期:{tdate or '—'}({expired}) 创建:{created}")
                lines.append(line)
            return "\n".join(lines)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"get_forecast 读取预测库失败: {e}")
        return f"系统预测读取失败: {e}"


def _read_opportunities(db: Session, limit: int = 10) -> str:
    """读取今日机会候选(主库 entry_candidates, active 且有信号, 取最新日期, 按得分降序)。"""
    latest = (
        db.query(func.max(EntryCandidate.snapshot_date))
        .filter(EntryCandidate.status == "active")
        .scalar()
    )
    if not latest:
        return "暂无机会候选（今日没有 active 候选）。"
    total = (
        db.query(func.count(EntryCandidate.id))
        .filter(EntryCandidate.status == "active", EntryCandidate.snapshot_date == latest)
        .scalar()
    )
    rows = (
        db.query(EntryCandidate)
        .filter(
            EntryCandidate.status == "active",
            EntryCandidate.snapshot_date == latest,
            EntryCandidate.signal.isnot(None),
            EntryCandidate.signal != "",
        )
        .order_by(EntryCandidate.score.desc())
        .limit(max(1, min(int(limit), 50)))
        .all()
    )
    if not rows:
        return f"今日({latest})暂无带信号的机会候选（共{total}条 active，均无 signal）。"
    lines = [f"【今日机会候选】{latest} 共{total}条active，按得分Top{len(rows)}:"]
    for c in rows:
        target = c.target_price
        target_str = f"{target:.2f}" if isinstance(target, (int, float)) else "—"
        lines.append(
            f"- {c.stock_symbol} {c.stock_name} 得分{c.score:g} 操作:{c.action_label} "
            f"信号:{c.signal} 目标价:{target_str}"
        )
    return "\n".join(lines)


async def _read_sentiment_cycle() -> str:
    """情绪周期判别(2026-08-23 F1 接线): 接 MarketSentimentCollector 取涨停池
    指标 → classify_sentiment_cycle(此前为死代码, 生产零引用)。"""
    from src.core.sentiment_cycle import classify_sentiment_cycle, format_cycle
    from src.core.report_generator import _collect_limit_up_summary

    summary = await _collect_limit_up_summary()
    if not isinstance(summary, dict) or summary.get("error"):
        return "情绪周期: 涨停池数据获取失败, 暂无法判别短线情绪周期。"

    metrics = {
        "limit_up_count": summary.get("total"),
        "max_board_height": summary.get("max_days"),
        "break_rate": summary.get("break_rate"),
        "yesterday_board_perf": summary.get("yesterday_board_perf"),
        "losing_effect": summary.get("losing_effect"),
    }
    result = classify_sentiment_cycle(metrics)
    return "短线情绪周期: " + format_cycle(result)


def _read_strategy_signals(db: Session, limit: int = 10) -> str:
    """读取最新策略信号(主库 strategy_signal_runs, active 且动作属买/关注类, 取最新日期, 按得分降序)。"""
    action_whitelist = ("buy", "watch", "hold", "alert")  # 买/关注/持有/告警类信号
    latest = (
        db.query(func.max(StrategySignalRun.snapshot_date))
        .filter(
            StrategySignalRun.status == "active",
            StrategySignalRun.action.in_(action_whitelist),
        )
        .scalar()
    )
    if not latest:
        return "暂无策略信号（今日没有 active 的买/关注类信号）。"
    rows = (
        db.query(StrategySignalRun)
        .filter(
            StrategySignalRun.status == "active",
            StrategySignalRun.snapshot_date == latest,
            StrategySignalRun.action.in_(action_whitelist),
        )
        .order_by(StrategySignalRun.score.desc())
        .limit(max(1, min(int(limit), 50)))
        .all()
    )
    if not rows:
        return f"最新交易日({latest})暂无买/关注类策略信号。"
    lines = [f"【策略信号】{latest} 最新active买/关注类信号 Top{len(rows)}:"]
    for s in rows:
        score = f"{s.score:g}" if isinstance(s.score, (int, float)) else str(s.score or "—")
        lines.append(
            f"- {s.stock_symbol} {s.stock_name} 策略:{s.strategy_name or s.strategy_code} "
            f"动作:{s.action_label}({s.action}) 得分:{score} 信号:{s.signal or '—'}"
        )
    return "\n".join(lines)


def _read_notifications(db: Session, limit: int = 10, unread_only: bool = False) -> str:
    """读取最近通知(主库 notifications, 按时间倒序; unread_only 时只取未读)。"""
    q = db.query(Notification)
    if unread_only:
        q = q.filter(Notification.read_at.is_(None))
    total = q.count()
    if total == 0:
        return "暂无通知" + ("（无未读通知）" if unread_only else "") + "。"
    rows = q.order_by(Notification.created_at.desc()).limit(max(1, min(int(limit), 50))).all()
    lines = [f"【系统通知】最近{len(rows)}条" + ("（未读）" if unread_only else "") + f"（共{total}条）:"]
    for n in rows:
        ts = n.created_at.strftime("%Y-%m-%d %H:%M") if n.created_at is not None else ""
        unread = "未读" if n.read_at is None else "已读"
        body = (n.body or "").strip().replace("\n", " ")
        body = body[:50] + ("…" if len(body) > 50 else "")
        lines.append(f"- [{ts}] {n.title} 类型:{n.category}/{n.level} {unread} {body}")
    return "\n".join(lines)


# ──────────────── 个股基本面明细工具(2026-08-13): 龙虎榜/两融/股东/分红/事件 ────────────────


def _fmt_yi(v) -> str:
    """元 → 亿(2位小数); None → —。"""
    if v is None:
        return "—"
    try:
        return f"{float(v) / 1e8:,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _fmt_num(v) -> str:
    """千分位整数; None → —。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):,.0f}"
    except (TypeError, ValueError):
        return str(v)


def _format_fundamentals_text(symbol: str, market: str, data: dict) -> str:
    """把 fetch_fundamentals_detail 的 dict 渲染成对话助手可读文本(无数据明确说「暂无」)。"""
    lines = [f"【{symbol} 基本面明细】(市场 {market})"]

    # 1) 龙虎榜(近10日)
    dt = data.get("dragon_tiger") or []
    lines.append(f"■ 龙虎榜(近10日): {len(dt)}条" if dt else "■ 龙虎榜(近10日): 暂无")
    for r in dt[:8]:
        chg = r.get("change_pct")
        chg_str = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "—"
        reason = r.get("reason") or "—"
        lines.append(
            f"- {r.get('trade_date') or '—'} 收盘{_fmt_num(r.get('close'))} "
            f"涨跌{chg_str} 净买{_fmt_yi(r.get('net_buy'))}亿 "
            f"买入{_fmt_yi(r.get('buy_amt'))}亿 卖出{_fmt_yi(r.get('sell_amt'))}亿 原因:{reason}"
        )

    # 2) 融资融券
    mg = data.get("margin") or []
    lines.append(f"■ 融资融券: {len(mg)}条" if mg else "■ 融资融券: 暂无")
    for r in mg[:3]:
        lines.append(
            f"- {r.get('date') or '—'} 融资余额{_fmt_yi(r.get('rz_balance'))}亿 "
            f"融券余额{_fmt_yi(r.get('rq_balance'))}亿 两融合计{_fmt_yi(r.get('total_balance'))}亿 "
            f"融资买入{_fmt_yi(r.get('rz_buy'))}亿 融资偿还{_fmt_yi(r.get('rz_repay'))}亿"
        )

    # 3) 股东户数
    sh = data.get("shareholders") or []
    lines.append(f"■ 股东户数: {len(sh)}期" if sh else "■ 股东户数: 暂无")
    for r in sh[:3]:
        cr = r.get("change_ratio")
        cr_str = f"{cr:+.2f}%" if isinstance(cr, (int, float)) else "—"
        cn = r.get("change_num")
        cn_str = f"{int(cn):+,}" if isinstance(cn, (int, float)) else "—"
        lines.append(
            f"- {r.get('report_date') or '—'} 户数{_fmt_num(r.get('holder_num'))} "
            f"较上期{cn_str}户(环比{cr_str}) 户均持股{_fmt_num(r.get('avg_shares'))}"
        )

    # 4) 分红
    dv = data.get("dividend") or []
    lines.append(f"■ 分红: {len(dv)}次" if dv else "■ 分红: 暂无")
    for r in dv[:8]:
        dps = r.get("dividend_per_share")
        dps_str = f"{dps:.2f}元" if isinstance(dps, (int, float)) else "—"
        tf = r.get("transfer_ratio")
        bf = r.get("bonus_ratio")
        tf_str = f"{tf:g}" if isinstance(tf, (int, float)) else "—"
        bf_str = f"{bf:g}" if isinstance(bf, (int, float)) else "—"
        lines.append(
            f"- {r.get('ex_date') or '—'} 每股派息{dps_str} 每10股转增{tf_str} "
            f"每10股送股{bf_str} [{r.get('progress') or '—'}]"
        )

    # 5) 事件日历(近7日)
    ev = data.get("events") or []
    lines.append(f"■ 事件日历(近7日): {len(ev)}条" if ev else "■ 事件日历(近7日): 暂无")
    for r in ev[:10]:
        ts = (r.get("publish_time") or "")[:10]
        src = r.get("source") or ""
        lines.append(f"- [{ts}] {r.get('title') or '—'} ({src})")

    return "\n".join(lines)


# ──────────────── 网页链接抓取工具(2026-08-14): get_web_content ────────────────

# SSRF 防护: 内网/本地/云 metadata 主机名(IP 直连 + 域名解析后双重检查)
_INTERNAL_HOSTNAMES = {
    "localhost", "metadata.google.internal", "metadata.tencentyun.com",
    "metadata.aliyun.com", "metadata", "kubernetes.default.svc",
}


def _is_internal_target(parsed) -> bool:
    """判断目标 URL 是否指向内网/本地/云 metadata(SSRF 拦截)。"""
    import ipaddress
    import socket

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host in _INTERNAL_HOSTNAMES:
        return True
    # IP 形式直接判断
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        pass
    # 域名形式: 解析一次, 命中内网段也拒绝(防 DNS 指向内网)
    try:
        for info in socket.getaddrinfo(host, None):
            try:
                ip = ipaddress.ip_address(info[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    return True
            except ValueError:
                continue
    except (socket.gaierror, OSError):
        pass  # 解析失败交给后续请求报错
    return False
# 用户可能在对话中发来网页链接(微信公众号文章/新闻/研报等), AI 通过该工具抓取正文再回答。
# 轻量实现: httpx GET(15s 超时 + 常见浏览器 UA, 微信文章需要 UA) + html.parser 标准库提取正文,
# 不引入 BeautifulSoup 等重型依赖。

_WEB_CONTENT_MAX_CHARS = 3000              # 返回给 LLM 的正文截断上限
_WEB_CONTENT_MAX_BYTES = 2 * 1024 * 1024   # 响应体读取上限, 防异常大页面拖垮
_WEB_FETCH_TIMEOUT = 15                    # 秒
_WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 块级/换行标签: 提取文本时在标签边界补换行, 避免正文挤成一行
_WEB_BLOCK_TAGS = frozenset({
    "p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "section", "article", "blockquote", "pre", "ul", "ol", "table", "hr",
})
# 跳过标签: 内部文本不参与提取(脚本/样式/头部/导航/页脚/内联框架/表单等噪音)
_WEB_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "head", "title", "meta", "link",
    "iframe", "svg", "nav", "footer", "header", "form", "button",
    "template", "video", "audio", "canvas", "aside",
})
# HTML 空元素(void): 没有闭合标签, 深度计数必须跳过, 否则 head 内的 <link>/<meta>
# 会把 _skip_depth 永久抬高, 导致 body 正文被误判为噪音而全部丢弃
_HTML_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})


class _WebTextExtractor(html.parser.HTMLParser):
    """轻量 HTML 正文提取器: 跳过 script/style 等噪音标签, 块级标签边界补换行。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _WEB_SKIP_TAGS and tag not in _HTML_VOID_TAGS:
            self._skip_depth += 1
        if self._skip_depth == 0 and tag in _WEB_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _WEB_SKIP_TAGS and tag not in _HTML_VOID_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
        elif self._skip_depth == 0 and tag in _WEB_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self.parts.append(data)


def _extract_web_text(html_text: str) -> str:
    """从 HTML 提取正文文本: 去噪音标签 → 折叠空白 → 去空行。解析异常不致命, 用已收集部分。"""
    parser = _WebTextExtractor()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        pass
    lines = []
    for ln in re.split(r"\n+", "".join(parser.parts)):
        ln = re.sub(r"[ \t\u00a0]+", " ", ln).strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)


def get_web_content(url: str) -> str:
    """抓取网页链接正文文本, 供 AI 分析用户发来的链接(含微信公众号文章 mp.weixin.qq.com)。

    安全/健壮性: 仅允许 http/https; 15s 超时; 常见浏览器 UA; 响应体上限 2MB;
    正文截断到 3000 字符返回; 任何失败均返回友好错误文本, 不抛异常。
    """
    url = (url or "").strip()
    if not url:
        return "抓取失败: 链接为空, 请提供有效的 http/https 网址。"
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return "抓取失败: 链接格式非法, 仅支持 http/https 网址。"
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "抓取失败: 仅支持 http/https 链接, 请检查链接格式。"

    # SSRF 防护(2026-08-15): 拒绝内网/本地/云 metadata 地址, 防服务器被当作代理扫描内网
    if _is_internal_target(parsed):
        return "抓取失败: 目标链接为内网/本地地址, 已拒绝访问。"

    try:
        import httpx
    except ImportError:
        return "抓取失败: 当前环境缺少 httpx 依赖, 无法发起网络请求。"

    try:
        headers = {
            "User-Agent": _WEB_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        raw = b""
        encoding = "utf-8"
        with httpx.Client(timeout=_WEB_FETCH_TIMEOUT, follow_redirects=True, headers=headers) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                ctype = (resp.headers.get("content-type") or "").lower()
                if ctype and not any(k in ctype for k in ("text/", "html", "xhtml", "xml", "json")):
                    return (
                        "抓取失败: 目标链接返回的不是网页内容"
                        f"(Content-Type: {ctype.split(';')[0].strip()}), 无法提取正文。"
                    )
                for chunk in resp.iter_bytes():
                    raw += chunk
                    if len(raw) > _WEB_CONTENT_MAX_BYTES:
                        return "抓取失败: 页面超过 2MB 读取上限, 已放弃抓取(可能为异常大页面)。"
                encoding = resp.encoding or "utf-8"
        try:
            html_text = raw.decode(encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            html_text = raw.decode("utf-8", errors="replace")
        text = _extract_web_text(html_text)
        if not text:
            return f"抓取失败: 页面未提取到正文文本({url})。"
        if len(text) > _WEB_CONTENT_MAX_CHARS:
            text = text[:_WEB_CONTENT_MAX_CHARS] + "…[已截断]"
        return f"【网页内容】{url}\n{text}"
    except httpx.HTTPStatusError as e:
        return f"抓取失败: 目标链接返回 HTTP {e.response.status_code}。"
    except httpx.TimeoutException:
        return "抓取失败: 请求超时(15s), 链接可能不可达或响应过慢。"
    except httpx.RequestError as e:
        return f"抓取失败: 网络请求错误({e.__class__.__name__}: {str(e)[:120]})。"
    except Exception as e:
        logger.warning(f"get_web_content 抓取失败 [{url}]: {e}")
        return f"抓取失败: {str(e)[:120]}。"


async def _execute_tool(
    db: Session, name: str, args: dict, user: User | None = None
) -> str:
    """执行工具调用，返回结果文本。

    修复 2026-08-21(国内生产): get_market_news 等分支的局部 `import asyncio`
    使 asyncio 成为整个函数作用域的局部名 → get_main_intent / get_rally_analysis
    等分支引用 asyncio 时抛 UnboundLocalError(线上表现: "主力意图获取失败:
    cannot access local variable 'asyncio'")。在函数入口统一 import 一次,
    所有分支可用; 各分支内的重复局部 import 变为冗余但无害。

    S5(2026-08-26): user 沿工具链下传, 数据类工具(get_portfolio/get_stock_suggestions/
    get_watchlist)只读当前用户自己的数据。
    """
    import asyncio
    try:
        if name == "get_portfolio":
            result = _build_portfolio_context(db, user=user)
            return result or "用户暂无持仓。"
        elif name == "get_stock_quote":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            result = await _fetch_realtime_context(symbol, market)
            return result or f"未能获取 {market}:{symbol} 的行情数据。"
        elif name == "get_technical_analysis":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            result = await _fetch_technical_context(symbol, market)
            return result or f"未能获取 {market}:{symbol} 的技术面数据。"
        elif name == "get_main_intent":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            if market != "CN":
                return "主力意图仅支持 A 股(CN)。"
            try:
                from src.agents.intraday_monitor import _main_intent_summary
                # 热修 2026-08-14: 同步网络调用包 to_thread, 防阻塞 asyncio 事件循环(登录超时根因)
                result = await asyncio.to_thread(_main_intent_summary, symbol)
                # 数据源口径标注(腾讯逐笔·主力意图), 用户可见, 避免与东财四档混淆
                return f"[数据源: 腾讯逐笔·主力意图口径]\n{result}" if result else f"未能获取 {symbol} 的主力意图数据。"
            except Exception as e:
                return f"主力意图获取失败: {str(e)[:100]}"
        elif name == "get_decision_pioneer":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            if market != "CN":
                return "决策先锋三指标仅支持 A 股(CN)。"
            try:
                from src.core.decision_pioneer import decision_pioneer_text
                # 热修: 同步网络调用包 to_thread, 防阻塞事件循环
                result = await asyncio.to_thread(decision_pioneer_text, symbol, market)
                return f"[数据源: 决策先锋三指标·机构活跃度+GS+L2主力净流入]\n{result}" if result else f"未能获取 {symbol} 的决策先锋数据。"
            except Exception as e:
                return f"决策先锋获取失败: {str(e)[:100]}"
        elif name == "get_rally_analysis":
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
        elif name == "get_stock_suggestions":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            result = _build_stock_context(db, symbol, market, user=user)
            return result or f"暂无 {market}:{symbol} 的 AI 建议。"
        elif name == "get_watchlist":
            return _build_watchlist_context(db, user=user)
        elif name == "get_capital_flow":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            result = await _fetch_capital_flow_context(symbol, market)
            # 数据源口径标注(东财四档·资金流向), 与 get_main_intent(逐笔) 区分
            return f"[数据源: 东财四档·资金流向口径]\n{result}" if result else f"未能获取 {market}:{symbol} 的资金流向数据。"
        elif name == "tdx_wenda":
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
        elif name == "get_market_news":
            limit = int((args.get("limit") or 15))
            briefing_type = (args.get("briefing_type") or "").strip()
            try:
                import asyncio

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
        elif name == "get_kline_patterns":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            return await _fetch_kline_pattern_context(symbol, market)
        elif name == "get_auction_data":
            scene = args.get("scene", "overview")
            limit = int(args.get("limit", 10) or 10)
            return await _fetch_auction_context(scene, limit)
        elif name == "get_forecast":
            symbol = (args.get("symbol") or "").strip()
            limit = int(args.get("limit", 5) or 5)
            return _read_forecast(symbol, limit)
        elif name == "get_opportunities":
            limit = int(args.get("limit", 10) or 10)
            return _read_opportunities(db, limit)
        elif name == "get_sentiment_cycle":
            return await _read_sentiment_cycle()
        elif name == "get_strategy_signals":
            limit = int(args.get("limit", 10) or 10)
            return _read_strategy_signals(db, limit)
        elif name == "get_notifications":
            limit = int(args.get("limit", 10) or 10)
            unread_only = bool(args.get("unread_only") or False)
            return _read_notifications(db, limit, unread_only)
        elif name == "get_fundamentals_detail":
            symbol = (args.get("symbol") or "").strip()
            market = args.get("market", "CN")
            if not symbol:
                return "请提供股票代码(symbol)。"
            try:
                # 局部 import asyncio: _execute_tool 内 get_market_news 分支已有
                # `import asyncio`, 使 asyncio 成为整个函数作用域的局部名, 顶层 import 不可见
                import asyncio
                # 懒加载避免模块级循环依赖; 同步取数放线程池不阻塞事件循环
                from src.web.api.market_data import fetch_fundamentals_detail

                data = await asyncio.to_thread(fetch_fundamentals_detail, symbol, market)
                return _format_fundamentals_text(symbol, market, data)
            except Exception as e:
                logger.warning(f"get_fundamentals_detail 工具失败 [{symbol}]: {e}")
                return f"基本面明细查询失败: {e}"
        elif name == "get_irm_qa":
            # 互动易问答(巨潮 cninfo): 公司对投资者提问的官方回应, 验证传闻/利好的权威信源
            symbol = (args.get("symbol") or "").strip()
            market = args.get("market", "CN")
            if not symbol:
                return "请提供股票代码(symbol)。"
            if market != "CN":
                return "互动易问答仅支持 A 股(CN)。"
            try:
                # 局部 import asyncio: 本函数 get_market_news 分支已有局部 `import asyncio`,
                # 使 asyncio 成为整个函数作用域的局部名, 顶层 import 不可见, 必须在此分支内重新 import
                import asyncio
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
        elif name == "get_market_anomalies":
            # 东财异动池: 交易所「严重异常波动」口径, 市场级(无视个股参数)
            limit = min(int(args.get("limit", 20) or 20), 50)
            try:
                import asyncio
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
        elif name == "get_northbound":
            # 北向资金(同花顺口径): 当日沪股通净额(亿元)。
            # 2024-08 后交易所停止披露实时净买入, 同花顺估算口径仅供参考; 深股通暂缺。
            try:
                import asyncio
                from src.core.data_collector import DataCollectorManager

                mgr = DataCollectorManager()

                def _fetch_nb():
                    from src.web.database import SessionLocal
                    from src.web.models import DataSource

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
        elif name == "get_hot_stocks":
            # 同花顺热榜(小时榜/日榜): 排名/热度/概念标签/AI归因(analyse)
            period = (args.get("period") or "hour").strip().lower()
            if period not in ("hour", "day"):
                period = "hour"
            limit = min(int(args.get("limit", 20) or 20), 50)
            try:
                import asyncio
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
        elif name == "get_web_content":
            url = (args.get("url") or "").strip()
            if not url:
                return "请提供要抓取的网页链接(url)。"
            # 局部 import asyncio: 函数内 get_market_news 分支有 `import asyncio`,
            # 使 asyncio 成为整个函数作用域的局部名, 必须在本分支内重新 import 才能使用
            import asyncio
            # 同步网络抓取放线程池, 不阻塞事件循环(与 2026-08-14 热修风格一致)
            return await asyncio.to_thread(get_web_content, url)
        elif name in _THSDK_TOOL_NAMES_SET:
            # thsdk 11 个高价值工具(2026-08-20, 选项 C): 通过 tools_thsdk 间接调用 thsdk_l2
            result = await _exec_thsdk_tool(name, args)
            return result
        elif name == "get_main_flow_compare":
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
        elif name == "get_delta_series":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            if market != "CN":
                return "秒级Delta序列分析仅支持 A 股(CN)。"
            try:
                import asyncio
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
        elif name == "get_orderbook":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            if market != "CN":
                return "盘口演变分析仅支持 A 股(CN)。"
            try:
                import asyncio
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
        elif name == "get_event_catalyst":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            if market != "CN":
                return "事件催化分析仅支持 A 股(CN)。"
            try:
                import asyncio
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
        elif name == "get_intent_explain":
            symbol = args.get("symbol", "")
            market = args.get("market", "CN")
            if market != "CN":
                return "主力意图解释仅支持 A 股(CN)。"
            try:
                import asyncio
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
        elif name == "get_factor_ic_report":
            market = args.get("market", "CN")
            if market != "CN":
                return "因子 IC 归因报告仅支持 A 股(CN)。"
            try:
                import asyncio
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
        else:
            return f"未知工具: {name}"
    except Exception as e:
        logger.error(f"工具执行失败 {name}: {e}")
        return f"工具执行出错: {e}"


def _summarize_old_messages(msgs: list) -> str:
    """把旧消息压缩成摘要(规则式, 不调 LLM 省成本)。

    策略: 只取 assistant 消息中含结论性关键词(结论/建议/综合/总体/因此/所以)
    的句子, 无结论句则取该消息最后一句; user 消息与空内容直接丢弃。
    返回「【早期对话摘要】...」文本; 无可用内容时返回空串。
    """
    keywords = ("结论", "建议", "综合", "总体", "因此", "所以")
    lines: list[str] = []
    for m in msgs:
        if m.role != "assistant" or not m.content:
            continue
        content = m.content.strip()
        # 按句号/感叹号/问号/换行切句
        sentences = [s.strip() for s in re.split(r"[。！？!?；;\n]", content) if s.strip()]
        if not sentences:
            continue
        picked = None
        for s in sentences:
            if any(kw in s for kw in keywords):
                picked = s
                break
        if picked is None:
            picked = sentences[-1]  # 无结论句 → 取最后一句兜底
        if len(picked) > 60:
            picked = picked[:60] + "…"
        lines.append(f"- {picked}")
    if not lines:
        return ""
    return "【早期对话摘要】(以下为较早对话的结论要点, 已压缩保留):\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# 重复提问守卫(借鉴 deepseek-harness 的 loop-hygiene guard)
# ---------------------------------------------------------------------------
# 检测最近用户消息中"同一股票 + 同一意图"的重复提问, 达到阈值时注入一条温和提醒。
# 只提醒、不阻断(veto); 用户新问题(不同股票/不同意图)即打断重置。
# 阈值 3 次起步, 可后续调大。
_REPEAT_QUESTION_THRESHOLD = 3  # 同股同意图连续出现次数阈值

# 常见问句意图词(按命中优先级排序: 长词/复合词在前, 避免被短词抢先截胡, 如"主力意图"先于"主力")
_REPEAT_QUESTION_INTENTS = (
    "主力意图", "资金流向", "龙虎榜", "基本面", "怎么看", "目标价",
    "分析", "预测", "主力", "资金", "持仓", "机会", "风险", "竞价",
    "形态", "公告", "新闻", "业绩", "估值", "支撑", "压力", "仓位",
    "买卖", "买", "卖", "涨", "跌", "点评", "诊断",
)

# 意图词别名归并: 复合词与词干语义相同, 归一为同一意图, 避免"主力意图"与"主力"被误判为不同意图
_REPEAT_QUESTION_INTENT_ALIASES = {
    "主力意图": "主力",
    "资金流向": "资金",
}


def _extract_repeat_stock_code(text: str) -> str | None:
    """从用户消息中提取 A 股 6 位股票代码(仅用于重复检测, 不校验存在性)。

    只认 0/3/4/6/8/9 开头的 6 位数字(沪深主板/创业板/科创板/北交所);
    前后不接数字, 排除日期(2026xxxx)、金额等常见误报;
    用 (?<!\d)(?!\d) 而非 \\b, 保证中文与代码紧邻("分析一下600519")也能提取。
    """
    m = re.search(r"(?<!\d)([036489]\d{5})(?!\d)", text)
    return m.group(1) if m else None


def _extract_repeat_intent(text: str) -> str | None:
    """从用户消息中提取问句意图词并归并别名(规范化用)。未命中任何意图词返回 None。"""
    for kw in _REPEAT_QUESTION_INTENTS:
        if kw in text:
            return _REPEAT_QUESTION_INTENT_ALIASES.get(kw, kw)
    return None


def _detect_repeat_question(history: list, threshold: int = 3) -> str | None:
    """检测"同股同意图"的重复提问, 命中返回温和提醒文案, 否则 None。

    借鉴 dsh loop-hygiene guard: 参数规范化后检测重复模式, 阈值渐次提醒、
    只提醒不阻断、用户新问题打断即重置。

    规则:
    - 输入: 最近用户消息列表(建议只传最近 ≤10 条 user 消息)
    - 规范化: 提取 6 位股票代码 + 意图词, 以 (代码, 意图) 为 key
    - 最近 threshold 条内, 当前消息的 key 累计出现 ≥threshold 次 → 返回提醒
    - 不同股票不算重复; 不同意图不算重复(分析→资金→形态属正常深化)
    - 零开销快速路径: 消息不足 threshold 条直接返回 None
    """
    # 快速路径: 样本不足阈值, 无需检测
    if len(history) < threshold:
        return None

    # 只看最近 threshold 条, 统计各 (股票, 意图) key 出现次数
    counts: dict[tuple[str, str], int] = {}
    last_key: tuple[str, str] | None = None
    for msg in history[-threshold:]:
        text = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
        if not text:
            continue
        stock = _extract_repeat_stock_code(text)
        intent = _extract_repeat_intent(text)
        if not stock or not intent:
            continue
        key = (stock, intent)
        counts[key] = counts.get(key, 0) + 1
        last_key = key

    # 以当前(最后一条)消息的 key 为准: 只有用户仍在问同类问题才提醒,
    # 用户换话题/换股票/换角度时自然不命中(打断即重置)
    if not last_key or counts.get(last_key, 0) < threshold:
        return None

    stock, intent = last_key
    return (
        f"【系统提示】你已连续 {threshold} 次询问 {stock} 的同类问题({intent}), "
        "是否已获得想要的答案? 如需新角度, 可以问: 主力意图/资金流向/技术形态/风险提示 等。"
    )


async def _describe_image(image_data: str, user=None) -> str:
    """视觉代理: 用「vision 场景」绑定的多模态模型看图生成文字描述。

    主对话模型(deepseek)无视觉能力, 图片先由视觉模型描述成文本,
    再拼进对话内容由主模型分析。视觉模型可在设置页「场景分配」随时更换。
    失败返回空串(调用方自行降级)。
    """
    try:
        from src.core.ai_client import get_model_for_scene
        from src.web.database import SessionLocal
        from src.web.models import AIService

        db = SessionLocal()
        try:
            # 1) vision 场景绑定优先(设置页可换)
            base_url, api_key, model_name = None, None, None
            try:
                model_obj = get_model_for_scene(db, "vision", user=user)
                if model_obj is not None:
                    svc = db.query(AIService).filter(AIService.id == model_obj.service_id).first()
                    if svc:
                        base_url, api_key, model_name = svc.base_url, svc.api_key, model_obj.model
            except Exception:
                pass
            # 2) 兜底: Agnes 服务 + agnes-2.5-flash(已知支持视觉)
            if not (base_url and api_key and model_name):
                svc = db.query(AIService).filter(AIService.name.like("%Agnes%")).first()
                if not svc:
                    return ""
                base_url, api_key, model_name = svc.base_url, svc.api_key, "agnes-2.5-flash"
        finally:
            db.close()

        import httpx

        payload = {
            "model": model_name,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "请用中文简要描述这张图片: 包含内容、颜色、形状、文字、图表类型等, 50字以内。",
                        },
                        {"type": "image_url", "image_url": {"url": image_data}},
                    ],
                }
            ],
            "max_tokens": 200,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            r.raise_for_status()
            return str(r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        logger.warning(f"视觉代理(看图)失败: {exc}")
        return ""


async def _build_ai_messages(
    db: Session, conv: ChatConversation, user: User, image_data: str | None = None
) -> list[dict]:
    """构建发送给 AI 的消息列表(system + 历史 + 数据上下文)。

    send_message(非流式)与 send_message_stream(流式)共用, 保证两条链路逻辑一致。
    """
    system_content = SYSTEM_PROMPT

    # 绑定股票提示
    if conv.stock_symbol and conv.stock_market:
        system_content += f"\n\n当前对话关联股票：{conv.stock_market}:{conv.stock_symbol}"

    # 用户交易风格画像(影子账户落库, 精简注入; 无画像则完全向后兼容)
    shadow_profile_block = _build_shadow_profile_block(getattr(user, "shadow_profile_json", None))
    if shadow_profile_block:
        system_content += "\n\n--- 用户交易风格画像 ---\n" + shadow_profile_block

    # 前端页面快照（对话创建时传入）
    if conv.initial_context:
        system_content += "\n\n--- 用户页面快照（对话创建时） ---\n" + conv.initial_context

    messages_for_ai: list[dict] = [{"role": "system", "content": system_content}]

    # 历史消息
    history = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conv.id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    # 上下文摘要滚动(2026-08-13): 超过 MAX_HISTORY_MESSAGES 时, 把最旧的消息压缩成摘要,
    # 保留最近 MAX_HISTORY_MESSAGES 条完整, 避免早期结论被挤出模型视野
    summary_block = ""
    if len(history) > MAX_HISTORY_MESSAGES:
        old_msgs = history[:-MAX_HISTORY_MESSAGES]
        summary_block = _summarize_old_messages(old_msgs)
        recent = history[-MAX_HISTORY_MESSAGES:]
    else:
        recent = history
    for m in recent:
        if m.role in ("user", "assistant"):
            messages_for_ai.append({"role": m.role, "content": m.content})

    # 重复提问守卫(借鉴 dsh loop-hygiene): 最近用户消息中同股同意图 ≥阈值 次时,
    # 注入一条温和提醒(只提醒不阻断)。仅追加到给模型的 messages_for_ai,
    # 不写 DB、不污染历史落库; send_message / send_message_stream 共用本函数, 一处修改两入口生效。
    repeat_hint = _detect_repeat_question(
        [m for m in recent if m.role in ("user",)][-10:], _REPEAT_QUESTION_THRESHOLD
    )
    if repeat_hint:
        logger.info("重复提问守卫触发: %s", repeat_hint.splitlines()[0][:60])
        messages_for_ai.append({"role": "user", "content": repeat_hint})

    # 注入基础上下文（持仓 + 绑定股票的行情/建议）— S5: 按当前用户过滤
    context_parts: list[str] = []

    # 用户持仓
    portfolio_ctx = _build_portfolio_context(db, user=user)
    if portfolio_ctx:
        context_parts.append(portfolio_ctx)

    # 绑定股票的实时数据
    if conv.stock_symbol and conv.stock_market:
        realtime = await _fetch_realtime_context(conv.stock_symbol, conv.stock_market)
        if realtime:
            context_parts.append(realtime)
        technical = await _fetch_technical_context(conv.stock_symbol, conv.stock_market)
        if technical:
            context_parts.append(technical)
        stock_ctx = _build_stock_context(db, conv.stock_symbol, conv.stock_market, user=user)
        if stock_ctx:
            context_parts.append(stock_ctx)

    if context_parts:
        # 把上下文追加到 system message
        messages_for_ai[0]["content"] += "\n\n--- 当前数据 ---\n" + "\n\n".join(context_parts)

    # 早期对话摘要注入 system prompt 末尾(如有压缩)
    if summary_block:
        messages_for_ai[0]["content"] += "\n\n" + summary_block

    # 多模态: 若本次消息带图片(base64 data URL), 把最后一条 user 消息替换为 content_parts(文本+图片)
    if image_data and messages_for_ai and messages_for_ai[-1].get("role") == "user":
        last_text = str(messages_for_ai[-1].get("content") or "")
        messages_for_ai[-1] = {
            "role": "user",
            "content": [
                {"type": "text", "text": last_text},
                {"type": "image_url", "image_url": {"url": image_data}},
            ],
        }
    return messages_for_ai


async def _run_tool_loop(
    ai_client: AIClient, messages_for_ai: list[dict], db: Session, user: User | None = None
):
    """带 tool use 的多轮对话(异步生成器)。

    产出两类事件:
    - ("stage", 阶段提示文案): 每个 tool 执行前产出, 供流式端点实时推送
    - ("text", 最终回复全文): 循环结束时产出, 且只会产出一次

    语义与原 send_message 内联循环完全等价(tool 不可用回落 chat_multi /
    轮次上限兜底 / 异常兜底), send_message 非流式路径同样消费本生成器。

    S5(2026-08-26): user 下传到 _execute_tool, 工具读数限本人数据。
    """
    try:
        for _round in range(MAX_TOOL_ROUNDS):
            try:
                response_msg = await ai_client.chat_with_tools(
                    messages_for_ai, tools=CHAT_TOOLS, temperature=0.5,
                )
            except Exception:
                # 模型不支持 tool use → 直接用 chat_multi
                logger.info("Tool use 不可用，使用普通对话")
                ai_response = await ai_client.chat_multi(messages_for_ai, temperature=0.5)
                yield "text", ai_response
                return

            if not response_msg.tool_calls:
                yield "text", (response_msg.content or "")
                return

            # 执行 tool calls
            messages_for_ai.append({
                "role": "assistant",
                "content": response_msg.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in response_msg.tool_calls
                ],
            })

            for tc in response_msg.tool_calls:
                tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                logger.info(f"Tool call: {tc.function.name}({tool_args})")
                yield "stage", _TOOL_STAGE_LABELS.get(tc.function.name, f"正在调用 {tc.function.name}...")
                result = await _execute_tool(db, tc.function.name, tool_args, user=user)
                messages_for_ai.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
        else:
            yield "text", (response_msg.content or "抱歉，处理轮次过多，请精简问题再试。")
    except Exception as e:
        logger.error(f"AI 对话失败: {e}")
        yield "text", f"抱歉，AI 服务暂时不可用：{e}"


def _sse_event(event: str, data: dict) -> str:
    """格式化一条 SSE 事件(event + data 两行, 空行结尾)。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _run_tool_loop_stream(ai_client, messages_for_ai, db, user: User | None = None):
    """流式 tool 循环(2026-08-23 U1 真流式): 边流式出字边执行工具。

    与 _run_tool_loop 等价, 但最终回答由 chat_with_tools_stream 单次调用
    边流式产出(delta), 不再"拿全文再假打字机"。产出事件:
    - ("stage", 文案): 工具执行前
    - ("delta", 正文增量): 最终回答实时增量
    - ("done", 全文): 结束时产出一次, 供落库

    S5(2026-08-26): user 下传到 _execute_tool, 工具读数限本人数据。
    """
    try:
        for _round in range(MAX_TOOL_ROUNDS):
            response_msg = None
            acc: list[str] = []
            try:
                async for kind, payload in ai_client.chat_with_tools_stream(
                    messages_for_ai, tools=CHAT_TOOLS, temperature=0.5
                ):
                    if kind == "delta":
                        acc.append(payload)
                        yield "delta", payload
                    else:
                        response_msg = payload
            except Exception:
                logger.info("流式 tool use 不可用，使用普通对话")
                ai_response = await ai_client.chat_multi(messages_for_ai, temperature=0.5)
                yield "delta", ai_response
                yield "done", ai_response
                return

            if response_msg is None or not response_msg.tool_calls:
                yield "done", "".join(acc)
                return

            # 执行 tool calls(与 _run_tool_loop 同款)
            messages_for_ai.append({
                "role": "assistant",
                "content": response_msg.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in response_msg.tool_calls
                ],
            })
            for tc in response_msg.tool_calls:
                tool_args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                logger.info(f"Tool call: {tc.function.name}({tool_args})")
                yield "stage", _TOOL_STAGE_LABELS.get(tc.function.name, f"正在调用 {tc.function.name}...")
                result = await _execute_tool(db, tc.function.name, tool_args, user=user)
                messages_for_ai.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
        else:
            yield "done", "抱歉，处理轮次过多，请精简问题再试。"
    except Exception as e:
        logger.error(f"AI 流式对话失败: {e}")
        yield "done", f"抱歉，AI 服务暂时不可用：{e}"


def _iter_text_chunks(text: str, size: int = 6):
    """把最终回复切成小块, 模拟打字机逐段输出。"""
    for i in range(0, len(text), size):
        yield text[i : i + size]


class CreateConversationBody(BaseModel):
    stock_symbol: str | None = None
    stock_market: str | None = None
    initial_context: str | None = None
    # 统一 LLM 配置中心(2026-08-13): AI 裁判等场景经 ai_model_id 指定会话模型,
    # send_message 的 _get_ai_client 优先用它(显式模型 > chat 场景绑定 > 默认)。
    ai_model_id: int | None = None


class SendMessageBody(BaseModel):
    content: str
    image_data: str | None = None  # 可选: 图片 base64 data URL(多模态, 模型看图)


def _client_from_scene_cfg(db: Session, cfg) -> AIClient | None:
    """把场景绑定配置归一为 AIClient(兼容多种返回形态), 无法识别返回 None。

    形态兼容(基础设施 A 子任务的 get_model_for_scene 未定型前尽量宽松):
    - dict 直给连接参数 {base_url, api_key, model}
    - dict 带 model_id/ai_model_id → 查 AIModel 行拼 AIClient
    - AIModel 实例 → 拼其 service
    - (AIModel, AIService) 元组
    """
    if not cfg:
        return None
    # 形态1: dict
    if isinstance(cfg, dict):
        base_url = cfg.get("base_url")
        model_name = cfg.get("model")
        if base_url and model_name:
            return AIClient(
                base_url=base_url,
                api_key=cfg.get("api_key") or "",
                model=model_name,
            )
        mid = cfg.get("model_id") or cfg.get("ai_model_id")
        if mid:
            m = db.query(AIModel).filter(AIModel.id == mid).first()
            if m:
                s = db.query(AIService).filter(AIService.id == m.service_id).first()
                if s:
                    return AIClient(base_url=s.base_url, api_key=s.api_key, model=m.model)
        return None
    # 形态2: AIModel 实例
    if isinstance(cfg, AIModel):
        s = db.query(AIService).filter(AIService.id == cfg.service_id).first()
        if s:
            return AIClient(base_url=s.base_url, api_key=s.api_key, model=cfg.model)
        return None
    # 形态3: (model, service) 元组
    if isinstance(cfg, (tuple, list)) and len(cfg) == 2:
        m, s = cfg[0], cfg[1]
        if isinstance(m, AIModel) and s is not None:
            return AIClient(
                base_url=getattr(s, "base_url", ""),
                api_key=getattr(s, "api_key", ""),
                model=m.model,
            )
    return None


def _get_ai_client(db: Session, model_id: int | None = None, user=None) -> AIClient:
    """获取 AI 客户端实例。

    模型选择优先级(2026-08-13 统一 LLM 配置中心; 2026-08-16 接入用户级解析):
    1. 会话显式指定模型(conv.ai_model_id —— AI 裁判等经 ai_model_id 建会话时用;
       用户级 granted 授权时校验该模型在授权列表内, 不在则回落 ②)
    2. 用户级解析 get_model_for_scene(db, "chat", user):
       BYOK 自有服务商 → 平台授权(从授权列表挑) → 全局 chat 场景绑定
    3. AIModel 表 is_default / 任意一条(无用户级配置时)
    4. Settings 默认配置
    """
    model = None
    service = None

    # 用户级 granted 授权列表(用于校验 conv.ai_model_id); BYOK 用户不限制平台模型
    granted_ids = None
    if user is not None:
        from src.core.ai_client import _get_model_access

        access = _get_model_access(user)
        if access is not None and access.get("mode") == "granted":
            granted_ids = set(access.get("model_ids") or [])

    # 1) 会话显式模型(裁判场景绑定等传入 ai_model_id 创建会话;
    #    granted 授权下模型不在列表内 → 视为不可用, 走 ② 用户级解析)
    if model_id and (granted_ids is None or model_id in granted_ids):
        model = db.query(AIModel).filter(AIModel.id == model_id).first()

    # 2) 用户级解析(BYOK/平台授权/chat 场景绑定); 函数未落地 → ImportError 自然回落
    if not model:
        try:
            from src.core.ai_client import get_model_for_scene

            scene_client = _client_from_scene_cfg(
                db, get_model_for_scene(db, "chat", user=user)
            )
            if scene_client is not None:
                return scene_client
        except Exception as e:
            logger.warning(f"chat 场景绑定不可用(回落 AIModel 默认): {e}")

    # 3) AIModel 默认/兜底
    if not model:
        model = db.query(AIModel).filter(AIModel.is_default == True).first()  # noqa: E712

    if not model:
        model = db.query(AIModel).first()

    if model:
        service = db.query(AIService).filter(AIService.id == model.service_id).first()

    if model and service:
        return AIClient(
            base_url=service.base_url,
            api_key=service.api_key,
            model=model.model,
            scene="chat",
        )

    settings = Settings()
    return AIClient(
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key,
        model=settings.ai_model,
        scene="chat",
    )


def _build_stock_context(db: Session, symbol: str, market: str, user: User | None = None) -> str:
    """为绑定股票构建上下文摘要。

    S5(2026-08-26): 传入 user 时按归属过滤建议/报告(NULL 视为共享),
    防止跨账号读取他人的 AI 建议与分析历史。
    """
    parts = []

    # 最近建议
    sug_query = db.query(StockSuggestion).filter(
        StockSuggestion.stock_symbol == symbol,
        StockSuggestion.stock_market == market,
    )
    if user is not None:
        sug_query = sug_query.filter(
            or_(
                StockSuggestion.user_id == user.id,
                StockSuggestion.user_id.is_(None),
            )
        )
    suggestions = (
        sug_query.order_by(StockSuggestion.created_at.desc())
        .limit(3)
        .all()
    )
    if suggestions:
        lines = []
        for s in suggestions:
            lines.append(f"- [{s.agent_label or s.agent_name}] {s.action_label}: {s.signal or s.reason or ''}")
        parts.append("最近 AI 建议：\n" + "\n".join(lines))

    # 最近分析报告
    hist_query = db.query(AnalysisHistory).filter(
        AnalysisHistory.stock_symbol == symbol
    )
    if user is not None:
        hist_query = hist_query.filter(
            or_(
                AnalysisHistory.user_id == user.id,
                AnalysisHistory.user_id.is_(None),
            )
        )
    histories = (
        hist_query.order_by(AnalysisHistory.created_at.desc())
        .limit(1)
        .all()
    )
    if histories:
        h = histories[0]
        content_preview = (h.content or "")[:500]
        parts.append(f"最近分析（{h.agent_name}, {h.analysis_date}）：\n{content_preview}")

    if not parts:
        return ""
    return "\n\n".join(parts)


def _build_portfolio_context(db: Session, user: User | None = None) -> str:
    """构建用户全部持仓摘要。

    S5(2026-08-26): 传入 user 时只返回本人持仓 + user_id=NULL 全局持仓,
    实盘(Position)与模拟盘(PaperTradingPosition)同样处理。
    """
    lines: list[str] = []

    # 实盘持仓
    pos_query = db.query(Position)
    if user is not None:
        pos_query = pos_query.filter(
            or_(Position.user_id == user.id, Position.user_id.is_(None))
        )
    positions = pos_query.all()
    if positions:
        real_lines = []
        for p in positions:
            stock = db.query(Stock).filter(Stock.id == p.stock_id).first()
            if not stock:
                continue
            real_lines.append(
                f"- {stock.name}({stock.market}:{stock.symbol}) "
                f"{p.quantity}股 成本{p.cost_price} 风格{p.trading_style or '波段'}"
            )
        if real_lines:
            lines.append("实盘持仓：\n" + "\n".join(real_lines))

    # 模拟盘持仓
    paper_query = db.query(PaperTradingPosition).filter(
        PaperTradingPosition.status == "open"
    )
    if user is not None and hasattr(PaperTradingPosition, "user_id"):
        paper_query = paper_query.filter(
            or_(
                PaperTradingPosition.user_id == user.id,
                PaperTradingPosition.user_id.is_(None),
            )
        )
    paper_positions = paper_query.all()
    if paper_positions:
        paper_lines = []
        for pp in paper_positions:
            pnl_str = f"浮盈{pp.unrealized_pnl:.1f}" if pp.unrealized_pnl else ""
            paper_lines.append(
                f"- {pp.stock_name or pp.stock_symbol}({pp.stock_market}:{pp.stock_symbol}) "
                f"{pp.quantity}股 入场价{pp.entry_price}"
                f"{f' 止损{pp.stop_loss}' if pp.stop_loss else ''}"
                f"{f' 目标{pp.target_price}' if pp.target_price else ''}"
                f"{f' {pnl_str}' if pnl_str else ''}"
            )
        if paper_lines:
            lines.append("模拟盘持仓：\n" + "\n".join(paper_lines))

    if not lines:
        return ""
    return "\n\n".join(lines)


async def _fetch_realtime_context(symbol: str, market: str) -> str:
    """异步获取实时行情和技术面。"""
    try:
        from src.core.marketdata_client import md_quote_rows
        from src.models.market import MarketCode

        mc = MarketCode(market) if market in ("CN", "HK", "US") else MarketCode.CN
        rows = await asyncio.to_thread(md_quote_rows, [symbol], mc.value)
        if not rows:
            return ""
        q = rows[0]
        price = q.get("current_price", "--")
        change = q.get("change_pct", "--")
        volume = q.get("volume", "--")
        name = q.get("name", symbol)
        return f"实时行情：{name}（{market}:{symbol}）价格 {price}，涨跌幅 {change}%，成交量 {volume}"
    except Exception as e:
        logger.debug(f"获取实时行情失败: {e}")
        return ""


async def _fetch_technical_context(symbol: str, market: str) -> str:
    """获取技术面摘要。"""
    try:
        from src.collectors.kline_collector import KlineCollector
        from src.models.market import MarketCode

        mc = MarketCode(market) if market in ("CN", "HK", "US") else MarketCode.CN
        collector = KlineCollector(mc)
        summary = await asyncio.to_thread(
            collector.get_kline_summary, symbol
        )
        if not summary or summary.get("error"):
            return ""
        # get_kline_summary 直接返回 summary 内容(无嵌套);兼容 API 层包装
        s = summary.get("summary", {}) if "summary" in summary else summary
        trend = s.get("trend", "--")
        macd = s.get("macd_status", "--")
        rsi = s.get("rsi_status") or (f"{s.get('rsi6')}" if s.get('rsi6') is not None else "--")
        support = s.get("support", "--")
        resistance = s.get("resistance", "--")
        # 形态
        pattern = s.get("kline_pattern") or "--"
        return f"技术面：趋势 {trend}，MACD {macd}，RSI {rsi}，支撑位 {support}，压力位 {resistance}，K线形态 {pattern}"
    except Exception as e:
        logger.debug(f"获取技术面失败: {e}")
        return ""


async def _fetch_capital_flow_context(symbol: str, market: str) -> str:
    """获取主力资金流向摘要（A股, 今日实时, 含四档分项）。"""
    try:
        from src.collectors.capital_flow_collector import CapitalFlowCollector
        from src.models.market import MarketCode

        mc = MarketCode(market) if market in ("CN", "HK", "US") else MarketCode.CN
        collector = CapitalFlowCollector(mc)
        summary = await asyncio.to_thread(
            collector.get_capital_flow_summary, symbol
        )
        if not summary or summary.get("error"):
            return ""

        def _fmt(v: float | None) -> str:
            """净额(元) → 亿/万 友好格式。"""
            if v is None:
                return "--"
            if abs(v) >= 1e8:
                return f"{v / 1e8:+.2f}亿"
            return f"{v / 1e4:+.0f}万"

        main = float(summary.get("main_net_inflow") or 0)
        direction = "净流入" if main > 0 else ("净流出" if main < 0 else "平衡")
        pct = summary.get("main_net_inflow_pct")
        # collector 已归一化为 %(f184 ×100 → %); None 显示 --
        pct_str = f"{float(pct):+.1f}%" if pct is not None else "--"
        flow_date = summary.get("date") or "最近交易日"

        lines = [f"资金流向（今日实时, 基准日 {flow_date}）"]
        lines.append(f"- 主力{direction} {_fmt(main)}（占比{pct_str}）")
        if summary.get("super_net_inflow") is not None:
            lines.append(
                f"- 超大单{_fmt(summary.get('super_net_inflow'))} | "
                f"大单{_fmt(summary.get('big_net_inflow'))} | "
                f"中单{_fmt(summary.get('mid_net_inflow'))} | "
                f"小单{_fmt(summary.get('small_net_inflow'))}"
            )
        if summary.get("trend_5d") and summary.get("trend_5d") != "无数据":
            lines.append(f"- 5日资金：{summary.get('trend_5d')}")
        # 分歧提示: 主力流入但超大单流出
        super_net = summary.get("super_net_inflow")
        if main > 0 and super_net is not None and float(super_net) < 0:
            lines.append("- ⚠️ 主力净流入但超大单净流出(分歧): 大单拉抬、超大单出货, 谨慎追涨")
        return "\n".join(lines)
    except Exception as e:
        logger.debug(f"获取资金流失败: {e}")
        return ""


async def _fetch_kline_pattern_context(symbol: str, market: str) -> str:
    """识别 K 线组合形态(同花顺教学体系 + TA-Lib 标准形态)。"""
    try:
        from src.core.marketdata_client import get_market_data
        from src.core.kline_pattern import detect_patterns, format_patterns
        from src.collectors.kline_collector import _detect_talib_patterns

        md = get_market_data()
        bars = await asyncio.to_thread(md.klines, symbol, market="CN" if market == "CN" else market, days=60)
        if not bars:
            return f"未能获取 {market}:{symbol} 的K线数据。"
        hits = detect_patterns(bars)
        text = format_patterns(hits)
        # TA-Lib 标准形态
        talib_hits = _detect_talib_patterns(list(bars))
        if talib_hits:
            text += "\n\n【TA-Lib 标准形态】"
            for p in talib_hits[:8]:
                text += f"\n- {p['cn_name']}({p['name']}) {p['signal']} 强度{p['strength']}"
        # 附带最近价格信息
        last = bars[-1]
        head = f"{market}:{symbol} 最近K线({last.date}): 开{last.open} 高{last.high} 低{last.low} 收{last.close}\n"
        return head + text
    except Exception as e:
        logger.debug(f"获取K线形态失败: {e}")
        return f"K线形态识别失败: {e}"


async def _fetch_auction_context(scene: str, limit: int = 10) -> str:
    """集合竞价数据(auction_collector: 悟道优先, 腾讯批量降级, 30s 缓存)。"""
    from src.collectors.auction_collector import (
        fetch_auction_overview,
        fetch_auction_strongest,
        fetch_auction_theme,
        fetch_auction_weak_to_strong,
        fetch_auction_risk,
    )

    scene = (scene or "overview").strip() or "overview"
    try:
        if scene in ("strongest", "watchlist"):
            return fetch_auction_strongest(limit=limit)
        if scene == "theme":
            return fetch_auction_theme(limit=limit)
        if scene == "weak_to_strong":
            return fetch_auction_weak_to_strong(limit=limit)
        if scene == "risk":
            return fetch_auction_risk(limit=limit)
        return fetch_auction_overview(limit=limit)
    except Exception as e:
        logger.debug(f"获取集合竞价失败: {e}")
        return f"集合竞价数据获取失败: {e}"


def _latest_unexpired_forecast_symbol(symbol: str = "") -> str:
    """读取预测库中最新一条未到期预测(target_date >= 今天)的股票代码。

    优先匹配传入的 symbol; 无匹配则取全局最新一条未到期预测。读取失败/无数据返回空串。
    """
    db_path = _resolve_forecast_db_path()
    if not os.path.exists(db_path):
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            cur = conn.cursor()
            tables = {r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            table = "forecasts" if "forecasts" in tables else ("prediction_runs" if "prediction_runs" in tables else None)
            if not table:
                return ""
            today = datetime.now().date().isoformat()
            if symbol:
                row = cur.execute(
                    f"SELECT symbol FROM {table} WHERE symbol = ? AND target_date >= ? ORDER BY created_at DESC LIMIT 1",
                    [symbol, today],
                ).fetchone()
                if row:
                    return row[0]
            row = cur.execute(
                f"SELECT symbol FROM {table} WHERE target_date >= ? ORDER BY created_at DESC LIMIT 1",
                [today],
            ).fetchone()
            return row[0] if row else ""
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"suggested_questions 读预测库失败: {e}")
        return ""


@router.get("/suggested-questions")
def suggested_questions(
    symbol: str = Query(..., description="股票代码"),
    market: str = Query("CN", description="市场"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """根据股票当前状态动态生成推荐问题（最多5条, 按优先级: 今日机会/系统预测/未读通知/持仓浮亏 → 通用模板兜底, 不调 AI）。

    S5(2026-08-26): 通知/持仓/建议等动态问题只看当前用户自己的数据(NULL 共享)。
    """
    questions: list[str] = []

    # ① 今日 active 机会候选(entry_candidates, 最新交易日且有信号) → 问机会
    latest = (
        db.query(func.max(EntryCandidate.snapshot_date))
        .filter(EntryCandidate.status == "active")
        .scalar()
    )
    if latest:
        has_signal = (
            db.query(EntryCandidate.id)
            .filter(
                EntryCandidate.status == "active",
                EntryCandidate.snapshot_date == latest,
                EntryCandidate.signal.isnot(None),
                EntryCandidate.signal != "",
            )
            .first()
        ) is not None
        if has_signal:
            questions.append("今天系统发现了什么机会？")

    # ③ 未读通知 → 问通知(S5: 仅本人 + 全局 NULL)
    unread = (
        db.query(Notification.id)
        .filter(
            Notification.read_at.is_(None),
            or_(Notification.user_id == user.id, Notification.user_id.is_(None)),
        )
        .first()
    )
    if unread:
        questions.append("今天的通知里有什么需要我关注的？")

    # ④ 持仓浮亏(简单判断: 模拟盘 open 且 unrealized_pnl < 0, 取浮亏最大的一只) → 问调仓
    losing_q = db.query(PaperTradingPosition).filter(
        PaperTradingPosition.status == "open",
        PaperTradingPosition.unrealized_pnl < 0,
    )
    if hasattr(PaperTradingPosition, "user_id"):
        losing_q = losing_q.filter(
            or_(
                PaperTradingPosition.user_id == user.id,
                PaperTradingPosition.user_id.is_(None),
            )
        )
    losing = losing_q.order_by(PaperTradingPosition.unrealized_pnl.asc()).first()
    if losing:
        questions.append(f"我的 {losing.stock_symbol} 持仓要调仓吗？")

    # ⑤ 兜底: 查最近建议(保持原有逻辑; S5: 仅本人 + NULL 共享行)
    latest_suggestion = (
        db.query(StockSuggestion)
        .filter(
            StockSuggestion.stock_symbol == symbol,
            StockSuggestion.stock_market == market,
            or_(
                StockSuggestion.user_id == user.id,
                StockSuggestion.user_id.is_(None),
            ),
        )
        .order_by(StockSuggestion.created_at.desc())
        .first()
    )
    if latest_suggestion:
        action = (latest_suggestion.action or "").lower()
        label = latest_suggestion.action_label or latest_suggestion.action or ""
        if action in ("buy", "add"):
            questions.append(f"最新的「{label}」信号可靠吗？入场时机如何？")
        elif action in ("sell", "reduce"):
            questions.append(f"最新给出了「{label}」建议，现在该操作吗？")
        elif action == "alert":
            questions.append("最近的异动提醒是什么情况？需要关注吗？")

    # 兜底: 查持仓（Position 通过 stock_id 关联 Stock 表）
    has_position = (
        db.query(Position)
        .join(Stock, Position.stock_id == Stock.id)
        .filter(Stock.symbol == symbol, Stock.market == market)
        .first()
    ) is not None
    if has_position:
        questions.append("当前持仓该继续持有还是考虑减仓？")
    else:
        questions.append("现在适合建仓吗？")

    # 通用问题(保持原有逻辑)
    questions.append("分析近期走势和关键支撑压力位")
    questions.append("有什么值得关注的消息或事件？")

    # 去重(优先保留靠前的动态问题) + 截断 5 条
    seen: set[str] = set()
    deduped: list[str] = []
    for q in questions:
        if q not in seen:
            seen.add(q)
            deduped.append(q)
    return {"questions": deduped[:5]}


@router.post("/conversations")
def create_conversation(
    body: CreateConversationBody | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S2(2026-08-23): 新建会话写入 user_id, 多账号各自只看到自己的会话。"""
    conv = ChatConversation(
        user_id=user.id,
        stock_symbol=body.stock_symbol if body else None,
        stock_market=body.stock_market if body else None,
        initial_context=body.initial_context if body else None,
        ai_model_id=body.ai_model_id if body else None,
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return {
        "id": conv.id,
        "title": conv.title or "",
        "stock_symbol": conv.stock_symbol,
        "stock_market": conv.stock_market,
        "created_at": str(conv.created_at or ""),
    }


@router.get("/conversations")
def list_conversations(
    limit: int = Query(30, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S2: 仅返回当前用户的会话; 缺失则返回空列表(空数据 ≠ 跨账号泄露)。"""
    rows = (
        db.query(ChatConversation)
        .filter(ChatConversation.user_id == user.id)
        .order_by(ChatConversation.updated_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": c.id,
            "title": c.title or "",
            "stock_symbol": c.stock_symbol,
            "stock_market": c.stock_market,
            "created_at": str(c.created_at or ""),
        }
        for c in rows
    ]


@router.get("/conversations/{conversation_id}")
def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S2: 仅返回当前用户的对话; 否则 404(防账号探测)。"""
    conv = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.id == conversation_id,
            ChatConversation.user_id == user.id,
        )
        .first()
    )
    if not conv:
        raise HTTPException(404, "对话不存在")
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at.asc())
        .all()
    )
    return {
        "conversation": {
            "id": conv.id,
            "title": conv.title or "",
            "stock_symbol": conv.stock_symbol,
            "stock_market": conv.stock_market,
            "created_at": str(conv.created_at or ""),
        },
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "created_at": str(m.created_at or ""),
            }
            for m in messages
        ],
    }


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """S2: 仅当前用户可删除对话; 否则 404(防账号探测)。"""
    conv = (
        db.query(ChatConversation)
        .filter(
            ChatConversation.id == conversation_id,
            ChatConversation.user_id == user.id,
        )
        .first()
    )
    if not conv:
        raise HTTPException(404, "对话不存在")
    db.query(ChatMessage).filter(ChatMessage.conversation_id == conversation_id).delete()
    db.delete(conv)
    db.commit()
    return {"ok": True}


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: int,
    body: SendMessageBody,
    user: User = Depends(get_current_user),
):
    """发送消息并获取 AI 回复（非流式，向后兼容）。

    S2(2026-08-23): 仅允许当前用户向自己的对话发消息; 越权访问返回 404 防账号探测。
    """
    db = SessionLocal()
    try:
        conv = (
            db.query(ChatConversation)
            .filter(
                ChatConversation.id == conversation_id,
                ChatConversation.user_id == user.id,
            )
            .first()
        )
        if not conv:
            raise HTTPException(404, "对话不存在")

        # demo 账号限流: 每日对话次数上限, 防共享模型 key 被公开访客滥用
        if user.username == "demo":
            from src.core.demo_limit import allow
            if not allow(user.id):
                raise HTTPException(429, "演示账号每日对话次数已用完(10次/天)。请自行部署体验完整功能: https://github.com/xiaoze-hub/Stock-Intelligent-Data-Analytics")

        # 多模态: 图片先由 agnes 视觉代理转成文字描述(在保存前处理, 保证 DB 历史连贯)
        if body.image_data:
            desc = await _describe_image(body.image_data, user=user)
            if desc:
                body.content = f"[用户附图内容] {desc}\n\n{body.content}"
                body.image_data = None  # 主模型用文本, 不传图片

        # 保存用户消息
        user_msg = ChatMessage(
            conversation_id=conversation_id,
            role="user",
            content=body.content,
        )
        db.add(user_msg)

        # 更新对话标题（首条消息取前 20 字）
        if not conv.title:
            conv.title = body.content[:20]

        db.commit()
        db.refresh(user_msg)

        # 构建消息列表 + 调用 AI（带 tool use，用于按需获取更多数据）
        messages_for_ai = await _build_ai_messages(db, conv, user, image_data=body.image_data)
        ai_client = _get_ai_client(db, conv.ai_model_id, user=user)
        ai_response = ""
        async for _kind, payload in _run_tool_loop(ai_client, messages_for_ai, db, user=user):
            if _kind == "text":
                ai_response = payload

        # 保存 AI 回复
        assistant_msg = ChatMessage(
            conversation_id=conversation_id,
            role="assistant",
            content=ai_response,
        )
        db.add(assistant_msg)

        # 更新对话时间
        conv.updated_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(assistant_msg)

        return {
            "id": assistant_msg.id,
            "role": "assistant",
            "content": assistant_msg.content,
            "created_at": str(assistant_msg.created_at or ""),
        }
    finally:
        db.close()


@router.post("/conversations/{conversation_id}/messages/stream")
async def send_message_stream(
    conversation_id: int,
    body: SendMessageBody,
    user: User = Depends(get_current_user),
):
    """发送消息并流式返回 AI 回复(SSE, text/event-stream)。

    事件类型(每行 event: xxx / data: json, 空行分隔):
    - stage: 阶段提示 {"message": "正在查询主力资金流向..."} — tool 执行前实时推送
    - delta: 回复正文增量 {"content": "..."} — 最终回复打字机效果
    - done:  回复落库完成 {"id","role","content","created_at"}
    - error: 流中断/异常 {"message": "..."}

    兼容性: 非流式 POST /messages 保持原样; 本端点仅在流式场景使用。
    消息落库与 send_message 一致: 用户消息在开始时保存, AI 回复全文在流结束时保存。
    """
    async def gen():
        db = SessionLocal()
        try:
            conv = (
                db.query(ChatConversation)
                .filter(
                    ChatConversation.id == conversation_id,
                    ChatConversation.user_id == user.id,
                )
                .first()
            )
            if not conv:
                yield _sse_event("error", {"message": "对话不存在"})
                return

            # demo 账号限流: 每日对话次数上限, 防共享模型 key 被公开访客滥用
            if user.username == "demo":
                from src.core.demo_limit import allow
                if not allow(user.id):
                    yield _sse_event("error", {"message": "演示账号每日对话次数已用完(10次/天)。请自行部署体验完整功能: https://github.com/xiaoze-hub/Stock-Intelligent-Data-Analytics"})
                    return

            # 多模态: 图片先由 agnes 视觉代理转成文字描述(在保存前处理, 保证 DB 历史连贯)
            if body.image_data:
                desc = await _describe_image(body.image_data, user=user)
                if desc:
                    body.content = f"[用户附图内容] {desc}\n\n{body.content}"
                    body.image_data = None

            # 保存用户消息
            user_msg = ChatMessage(
                conversation_id=conversation_id,
                role="user",
                content=body.content,
            )
            db.add(user_msg)

            # 更新对话标题（首条消息取前 20 字）
            if not conv.title:
                conv.title = body.content[:20]

            db.commit()
            db.refresh(user_msg)

            # 构建消息列表(与 send_message 共用逻辑)
            yield _sse_event("stage", {"message": "正在准备上下文..."})
            # 多模态: 图片先由 agnes 视觉代理转成文字描述, 再交给主对话模型
            if body.image_data:
                desc = await _describe_image(body.image_data, user=user)
                if desc:
                    body.content = f"[用户附图内容] {desc}\n\n{body.content}"
                    body.image_data = None
            messages_for_ai = await _build_ai_messages(db, conv, user, image_data=body.image_data)
            ai_client = _get_ai_client(db, conv.ai_model_id, user=user)

            # 多轮 tool use + 真流式(2026-08-23 U1): 边流式出字边执行工具
            ai_response = ""
            async for kind, payload in _run_tool_loop_stream(ai_client, messages_for_ai, db, user=user):
                if kind == "stage":
                    yield _sse_event("stage", {"message": payload})
                elif kind == "delta":
                    yield _sse_event("delta", {"content": payload})
                else:
                    ai_response = payload

            # 保存 AI 回复(全文落库, 与 send_message 一致)
            assistant_msg = ChatMessage(
                conversation_id=conversation_id,
                role="assistant",
                content=ai_response,
            )
            db.add(assistant_msg)

            # 更新对话时间
            conv.updated_at = datetime.now(timezone.utc)
            db.commit()
            db.refresh(assistant_msg)

            yield _sse_event("done", {
                "id": assistant_msg.id,
                "role": "assistant",
                "content": assistant_msg.content,
                "created_at": str(assistant_msg.created_at or ""),
            })
        except Exception as e:
            logger.error(f"流式对话失败: {e}")
            try:
                yield _sse_event("error", {"message": f"AI 服务暂时不可用：{e}"})
            except Exception:
                pass
        finally:
            db.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            # 声明 identity 编码, 让 GZipMiddleware 跳过压缩(否则小事件被 zlib 缓冲延迟推送)
            "Content-Encoding": "identity",
        },
    )
