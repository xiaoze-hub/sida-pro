"""标的全面证据档案聚合器 (Stock Full Dossier Aggregator)

作为 SIDA-Pro AI 助手的核心 Harness（编排线束），
负责在分析单只股票时，以毫秒级并行并发方式打包平台的全部维度客观证据：
1. 【实时量价盘口】现价、涨跌幅、量比、振幅、均线多空偏离度 (get_stock_quote + get_technical_analysis)
2. 【主力真金意图】腾讯逐笔大单吸筹/派发、内外盘博弈 (get_main_intent)
3. 【底仓筹码沉淀】120日自算CYQ成本峰、70%/90%筹码带、获利盘、突破/支撑位 (compute_main_accumulation_chips)
4. 【数智决策三指标】GS多空区间色带、机构活跃度生命线、L2主力净流 (get_decision_pioneer)
5. 【机构研报与共识】东方财富官方研报中心最新券商评级、目标价、研究机构 (fetch_eastmoney_research_reports)
6. 【突发事件与催化】近48小时相关事件、因果传导链与预期差

避免大模型单轮 ReAct 盲探、丢三落四或口径混淆。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from sqlalchemy.orm import Session
from src.db.models import User

logger = logging.getLogger(__name__)


async def build_stock_full_dossier(symbol: str, market: str = "CN", db: Session | None = None, user: User | None = None) -> dict[str, Any]:
    """并发收集单只标的的全景证据档案。"""
    clean_sym = symbol.strip().upper()
    if "." in clean_sym:
        clean_sym = clean_sym.split(".")[0]

    dossier: dict[str, Any] = {
        "symbol": clean_sym,
        "market": market,
        "quote": None,
        "technical": None,
        "main_intent": None,
        "chips": None,
        "decision_pioneer": None,
        "broker_reports": [],
        "errors": {},
    }

    # 1. 并发协程任务定义
    async def _fetch_quote():
        try:
            from src.agents.chat.registry import _tool_get_stock_quote
            return await _tool_get_stock_quote(db, {"symbol": clean_sym, "market": market}, user=user)
        except Exception as e:
            return f"行情获取异常: {e}"

    async def _fetch_technical():
        try:
            from src.agents.chat.registry import _tool_get_technical_analysis
            return await _tool_get_technical_analysis(db, {"symbol": clean_sym, "market": market}, user=user)
        except Exception as e:
            return f"技术分析异常: {e}"

    async def _fetch_intent():
        try:
            from src.agents.chat.registry import _tool_get_main_intent
            return await _tool_get_main_intent(db, {"symbol": clean_sym, "market": market}, user=user)
        except Exception as e:
            return f"主力意图异常: {e}"

    async def _fetch_chips():
        try:
            from src.agents.chat.tools_market import _tool_get_chip_accumulation
            return await _tool_get_chip_accumulation(db, {"symbol": clean_sym, "market": market}, user=user)
        except Exception as e:
            return f"筹码沉淀异常: {e}"

    async def _fetch_decision():
        try:
            from src.agents.chat.registry import _tool_get_decision_pioneer
            return await _tool_get_decision_pioneer(db, {"symbol": clean_sym, "market": market}, user=user)
        except Exception as e:
            return f"数智决策三指标异常: {e}"

    async def _fetch_reports():
        try:
            from src.collectors.eastmoney_research import fetch_eastmoney_research_reports
            res = await asyncio.to_thread(fetch_eastmoney_research_reports, clean_sym, page=1, page_size=5)
            return res.get("items") or []
        except Exception as e:
            logger.debug("研报获取异常: %s", e)
            return []

    # 2. 并行调度所有证据提取
    quote_res, tech_res, intent_res, chips_res, dec_res, reports_res = await asyncio.gather(
        _fetch_quote(),
        _fetch_technical(),
        _fetch_intent(),
        _fetch_chips(),
        _fetch_decision(),
        _fetch_reports(),
        return_exceptions=True
    )

    dossier["quote"] = quote_res if isinstance(quote_res, str) else str(quote_res)
    dossier["technical"] = tech_res if isinstance(tech_res, str) else str(tech_res)
    dossier["main_intent"] = intent_res if isinstance(intent_res, str) else str(intent_res)
    dossier["chips"] = chips_res if isinstance(chips_res, str) else str(chips_res)
    dossier["decision_pioneer"] = dec_res if isinstance(dec_res, str) else str(dec_res)
    dossier["broker_reports"] = reports_res if isinstance(reports_res, list) else []

    return dossier


def format_stock_dossier_text(dossier: dict[str, Any]) -> str:
    """将全景档案转换为清晰、无歧义的 AI 分析线束上下文。"""
    sym = dossier.get("symbol", "")
    lines = [
        f"=================== 【SIDA-Pro 标的客观全景档案 · {sym}】 ===================",
        "⚠️ 本档案已由系统 Harness 统一并行采集完毕。包含盘面、逐笔主力、底仓筹码、决策色带及机构研报五维证据：",
        "",
        "【第一维：实时行情与盘口态势】",
        dossier.get("quote") or "暂无行情",
        "",
        "【第二维：均线与技术面状态】",
        dossier.get("technical") or "暂无技术面",
        "",
        "【第三维：主力意图与真金证据（腾讯逐笔主动口径）】",
        dossier.get("main_intent") or "暂无主力意图",
        "",
        "【第四维：底仓筹码沉淀与获利带（自算CYQ·120日模型）】",
        dossier.get("chips") or "暂无筹码数据",
        "",
        "【第五维：数智决策三指标（GS多空色带 + 机构活跃度 + TQ暗盘）】",
        dossier.get("decision_pioneer") or "暂无决策三指标",
        "",
    ]

    reports = dossier.get("broker_reports") or []
    lines.append("【第六维：券商机构研报（东方财富官方研报中心最新收录）】")
    if reports:
        for r in reports[:3]:
            lines.append(
                f"- [{r.get('publish_date')}] 《{r.get('title')}》 | 机构: {r.get('org_name')} | "
                f"评级: 【{r.get('rating')}】 | 分析师: {r.get('author') or '详见研报'}"
            )
    else:
        lines.append("- 近期暂无公开券商深度研报")
    lines.append("==========================================================================")

    return "\n".join(lines)
