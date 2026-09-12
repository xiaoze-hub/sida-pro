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

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from src.agents.chat.registry import CHAT_TOOL_REGISTRY, chat_tool_schemas
from src.config import Settings
from src.core.ai_client import AIClient, LLMDegradedError
from src.web.api.auth import get_current_user, get_user_or_service
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
- 数智决策三指标(2026-08-30): 用户问「数智决策/三指标共振/GS策略/G买G卖/机构活跃度/AI机构活跃度/暗盘资金」时, 调用 get_decision_pioneer(机构活跃度+GS策略+L2主力净流入三合一)。「主力意图/吸筹派发」仍走 get_main_intent; 问「L2主力净流入」用 get_decision_pioneer 的 L2 字段(TQ口径), 问「东财四档资金流向」用 get_capital_flow。三者口径不同, 数字冲突时须说明口径差异, 不可混用下结论。
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
    "get_decision_pioneer": "正在分析数智决策三指标(GS/暗盘/机构活跃度)...",
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

# ──────────────── Tool Definitions(W3.3 起由注册表导出) ────────────────
# schema 与 handler 同处注册于 src/agents/chat/registry.py
# (ChatTool: schema + handler + caliber 口径标签 + requires 权限点);
# CHAT_TOOLS 仅保留导出面(既有测试/调用方兼容), 顺序 = 注册顺序。
CHAT_TOOLS = chat_tool_schemas()


async def _exec_thsdk_tool(name: str, args: dict) -> str:
    """执行 thsdk 工具(W3.3 起实现在 registry, 保留兼容入口供既有测试/调用方)。"""
    tool = CHAT_TOOL_REGISTRY.get(name)
    if tool is None:
        return f"[thsdk] 工具 {name} 尚未注册实现。"
    return await tool.handler(None, args, None)



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


def _read_notifications(
    db: Session, limit: int = 10, unread_only: bool = False, user: User | None = None
) -> str:
    """读取最近通知(主库 notifications, 按时间倒序; unread_only 时只取未读)。

    C3(2026-09-09): 传 user 时只读本人 + 全局(NULL)通知 —— 此前 AI 工具链
    get_notifications 会把所有用户的站内通知读给当前用户。
    """
    q = db.query(Notification)
    if unread_only:
        q = q.filter(Notification.read_at.is_(None))
    if user is not None:
        q = q.filter(
            or_(Notification.user_id == user.id, Notification.user_id.is_(None))
        )
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
    """执行工具调用(查注册表分发, W3.3 起 schema/handler 收口到 registry)。

    历史(2026-08-21): 工具分支的局部 `import asyncio` 曾使 asyncio 成为整个
    函数作用域的局部名 → 其他分支 UnboundLocalError; 拆到 registry 后每个
    handler 独立作用域且 registry 顶层已 import asyncio, 该类问题不复存在。

    S5(2026-08-26): user 沿工具链下传, 数据类工具(get_portfolio/get_stock_suggestions/
    get_watchlist/get_notifications)只读当前用户自己的数据(requires="self")。
    """
    tool = CHAT_TOOL_REGISTRY.get(name)
    if tool is None:
        return f"未知工具: {name}"
    try:
        return await tool.handler(db, args or {}, user)
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

    # 今日系统信号摘要(设计稿 §7.3「被动注入」, 盘后 cron 预生成, 读快照纯文本)
    # 无快照(早盘/未生成) → 不注入, 完全向后兼容; 数据缺失由块内 data_status 表达
    try:
        from src.core.signal_summary import read_latest_summary_text

        summary_text = read_latest_summary_text(db)
        if summary_text:
            system_content += "\n\n" + summary_text
    except Exception as e:  # noqa: BLE001
        logger.warning("[chat] 系统信号摘要注入失败(静默降级): %s", e)

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
    ai_client: AIClient, messages_for_ai: list[dict], db: Session,
    user: User | None = None, *, stream: bool = False,
):
    """带 tool use 的多轮对话(异步生成器, 流式/非流式统一, W3.3 合并)。

    产出事件:
    - ("stage", 阶段提示文案): 每个 tool 执行前, 供流式端点实时推送
    - stream=False: ("text", 最终回复全文) 循环结束时产出一次
    - stream=True:  ("delta", 正文增量) 由 chat_with_tools_stream 单次调用边
      流式产出, 结束时 ("done", 全文) 一次, 供落库

    语义: tool 不可用回落 chat_multi / LLMDegradedError 不回落(0.3, 防兜底重试
    放大) / 轮次上限兜底 / 异常兜底。send_message 非流式路径同样消费本生成器。
    S5(2026-08-26): user 下传到 _execute_tool, 工具读数限本人数据。
    """
    try:
        for _round in range(MAX_TOOL_ROUNDS):
            response_msg = None
            acc: list[str] = []
            try:
                if stream:
                    async for kind, payload in ai_client.chat_with_tools_stream(
                        messages_for_ai, tools=CHAT_TOOLS, temperature=0.5
                    ):
                        if kind == "delta":
                            acc.append(payload)
                            yield "delta", payload
                        else:
                            response_msg = payload
                else:
                    response_msg = await ai_client.chat_with_tools(
                        messages_for_ai, tools=CHAT_TOOLS, temperature=0.5,
                    )
            except LLMDegradedError:
                # 0.3: 服务降级≠工具不支持, 不许走 chat_multi 兜底重试放大
                raise
            except Exception:
                logger.info("流式 tool use 不可用，使用普通对话" if stream else "Tool use 不可用，使用普通对话")
                ai_response = await ai_client.chat_multi(messages_for_ai, temperature=0.5)
                if stream:
                    yield "delta", ai_response
                    yield "done", ai_response
                else:
                    yield "text", ai_response
                return

            if stream:
                no_calls = response_msg is None or not response_msg.tool_calls
            else:
                no_calls = not response_msg.tool_calls
            if no_calls:
                if stream:
                    yield "done", "".join(acc)
                else:
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
            if stream:
                yield "done", "抱歉，处理轮次过多，请精简问题再试。"
            else:
                yield "text", (response_msg.content or "抱歉，处理轮次过多，请精简问题再试。")
    except Exception as e:
        logger.error(f"AI {'流式' if stream else ''}对话失败: {e}")
        if stream:
            yield "done", f"抱歉，AI 服务暂时不可用：{e}"
        else:
            yield "text", f"抱歉，AI 服务暂时不可用：{e}"


def _sse_event(event: str, data: dict) -> str:
    """格式化一条 SSE 事件(event + data 两行, 空行结尾)。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"



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

    # 模拟盘持仓(2026-09-08 T6: user_id 列已落地, 按归属过滤; NULL 行为冷启动遗留, owner 可见)
    paper_query = db.query(PaperTradingPosition).filter(
        PaperTradingPosition.status == "open"
    )
    if user is not None:
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

        # 口径标签(B3/3.4): 与 get_main_intent(逐笔) 区分, 红线见 AGENTS.md
        from src.core.caliber import CAPITAL_FLOW_TAG

        lines = [
            f"资金流向（今日实时, 基准日 {flow_date}）{CAPITAL_FLOW_TAG.ui_label()}",
            f"- 口径说明: 本数据为按单金额四档归类, 非逐笔主动买卖方向, "
            f"禁止用于主力意图/吸筹派发判定; 主力意图一律以 get_main_intent(逐笔)为准, "
            f"两口径数字冲突时说明差异并优先采信逐笔。",
        ]
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
    # C3(2026-09-09): 删掉 hasattr(PaperTradingPosition, "user_id") 猜测式保护 ——
    # T6 迁移后 user_id 是真实列, 隔离按设计写, 不按"猜列存在与否"写。
    losing_q = db.query(PaperTradingPosition).filter(
        PaperTradingPosition.status == "open",
        PaperTradingPosition.unrealized_pnl < 0,
    )
    if user is not None:
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
    user=Depends(get_user_or_service),
):
    """S2(2026-08-23): 新建会话写入 user_id, 多账号各自只看到自己的会话。

    0.0(风险方案): 服务令牌(8010 AI 裁判)经 X-Service-Token 也可建会话,
    user_id=NULL(系统会话); 用户 JWT/服务令牌各自只能看到自己归属的会话。
    """
    is_service = getattr(user, "is_service", False)
    conv = ChatConversation(
        user_id=None if is_service else user.id,
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
    user=Depends(get_user_or_service),
):
    """发送消息并获取 AI 回复（非流式，向后兼容）。

    S2(2026-08-23): 仅允许当前用户向自己的对话发消息; 越权访问返回 404 防账号探测。
    0.0(风险方案): 服务令牌(8010 AI 裁判)经 X-Service-Token 只能访问
    user_id=NULL 的系统会话, 与用户会话隔离。
    """
    db = SessionLocal()
    try:
        owner_filter = (
            ChatConversation.user_id.is_(None)
            if getattr(user, "is_service", False)
            else ChatConversation.user_id == user.id
        )
        conv = (
            db.query(ChatConversation)
            .filter(
                ChatConversation.id == conversation_id,
                owner_filter,
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
    request: Request,
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
            # W3.3: 客户端断开即中止工具循环(停止 LLM 计费); 流被取消时清理上游
            ai_response = ""
            try:
                async for kind, payload in _run_tool_loop(
                    ai_client, messages_for_ai, db, user=user, stream=True
                ):
                    if await request.is_disconnected():
                        logger.info("客户端断开, 中止 LLM 工具循环(停止计费)")
                        break
                    if kind == "stage":
                        yield _sse_event("stage", {"message": payload})
                    elif kind == "delta":
                        yield _sse_event("delta", {"content": payload})
                    else:
                        ai_response = payload
            except asyncio.CancelledError:
                logger.info("流被取消, 清理上游 LLM 调用")
                raise

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
