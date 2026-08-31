"""SIDA 内置报告生成器(盘前/盘后)。

不依赖海外 Hermes cron: 直接调用 PanWatch 自身的数据能力采集数据,
经 LLM(场景 reports)生成结构化 Markdown 报告, 写入报告中心目录
(CRON_OUTPUT_DIR/<job_id>/YYYY-MM-DD_HH-MM-SS.md), 报告中心 API
(src/web/api/reports.py) 无需改动即可读取。

数据源(全部带降级, 拉不到显式标注"数据获取失败", 不编造):
- 涨停池/连板梯队: MarketSentimentCollector (wudao limit_up_filter → 东财 getTopicZTPool)
- 跌停: wudao limit_down → 东财 getTopicDTPool
- 大盘指数: marketdata index_quotes (腾讯)
- 两市资金流: 国内网关 /cn/market-overview (东财口径) → ths market_capital_flow
- 隔夜消息: marketdata flash_news (cls/ths 快讯流)
- 关注候选: entry_candidates 表 / 持仓: positions 表 / 策略信号: strategy_signal_runs 表

LLM 失败或未配置模型 → 模板拼纯数据报告(不编造)。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from src.core.timezone import beijing_now

logger = logging.getLogger(__name__)

# 固定 job_id(报告中心目录名, 与 Hermes cron 报告同源)
JOB_IDS = {
    "premarket": "premarket-daily",
    "postmarket": "postmarket-review",
}
REPORT_TITLES = {
    "premarket": "A股盘前报告",
    "postmarket": "A股盘后复盘",
}

# A 股大盘指数的显式腾讯符号(与 daily_report agent 口径一致)
_CN_INDEX_TENCENT_SYMBOLS = ["sh000001", "sz399001", "sz399006"]

# 两市资金流国内网关(东财口径, 与 src/web/api/market_data.py 同源)
_MARKET_OVERVIEW_URL = "http://115.190.177.213:8100/cn/market-overview"


def _report_root() -> Path:
    """报告中心根目录 = CRON_OUTPUT_DIR(与 reports.py list_reports 同源解析)。

    惰性 import: reports.py 在 import 时按环境变量解析目录, 保持一致保证
    "生成器写入的位置 = 报告中心读取的位置"。
    """
    from src.web.api.reports import CRON_OUTPUT_DIR

    return Path(CRON_OUTPUT_DIR)


def _db_session():
    from src.web.database import SessionLocal

    return SessionLocal()


# ──────────────────────────── 数据收集 ────────────────────────────


async def _collect_limit_up_summary() -> dict:
    """涨停家数/连板梯队/主线题材(wudao → 东财, 失败标注)。"""
    try:
        from src.collectors.market_sentiment_collector import MarketSentimentCollector

        return await asyncio.to_thread(
            MarketSentimentCollector().get_sentiment_summary
        )
    except Exception as e:
        logger.warning(f"[报告] 涨停复盘数据获取失败: {e}")
        return {"error": "数据获取失败"}


async def _collect_limit_down(date: str) -> list[dict]:
    """跌停池: wudao limit_down → 东财 getTopicDTPool。失败返回空并标注。"""
    # ① wudao
    try:
        from src.collectors.wudao_mcp_client import WudaoMCPClient

        resp = await asyncio.to_thread(
            WudaoMCPClient().call_tool, "limit_down", {"date": date, "limit": 30}
        )
        rows = []
        if isinstance(resp, dict):
            rows = resp.get("rows") or resp.get("items") or []
        if rows:
            out = []
            for it in rows:
                if not isinstance(it, dict):
                    continue
                out.append(
                    {
                        "code": str(it.get("code") or ""),
                        "name": str(it.get("name") or ""),
                        "days": it.get("continue_num") or it.get("continueNum") or 1,
                    }
                )
            return out
    except Exception as e:
        logger.debug(f"[报告] wudao 跌停池失败: {e}")
    # ② 东财跌停池
    try:
        from src.collectors.market_http import market_get

        data = await asyncio.to_thread(
            market_get,
            "https://push2ex.eastmoney.com/getTopicDTPool",
            host_key="push2ex.eastmoney.com",
            params={
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "dpt": "wz.ztzt",
                "Pageindex": "0",
                "pagesize": "30",
                "sort": "fbt:asc",
                "date": date,
            },
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Referer": "https://quote.eastmoney.com/",
            },
            timeout=10,
            retries=2,
            parse="json",
            log_label="跌停池",
        )
        if data:
            pool = (data.get("data") or {}).get("pool") or []
            return [
                {
                    "code": str(it.get("c") or ""),
                    "name": str(it.get("n") or ""),
                    "days": int(it.get("days", 1) or 1),
                }
                for it in pool
            ]
    except Exception as e:
        logger.debug(f"[报告] 东财跌停池失败: {e}")
    return []


async def _collect_indices() -> list[dict]:
    """大盘指数快照(腾讯, 稳)。失败返回空。"""
    try:
        from src.core.marketdata_client import get_market_data

        items = await asyncio.to_thread(
            get_market_data().index_quotes, _CN_INDEX_TENCENT_SYMBOLS
        )
        return [
            {
                "symbol": it.get("symbol", ""),
                "name": it.get("name", ""),
                "price": it.get("current_price"),
                "change_pct": it.get("change_pct"),
                "change_amount": it.get("change_amount"),
                "amount": it.get("turnover"),
            }
            for it in items
        ]
    except Exception as e:
        logger.warning(f"[报告] 大盘指数获取失败: {e}")
        return []


async def _collect_market_flow() -> dict:
    """两市资金流: 国内网关(东财口径) → ths 行业资金汇总。失败标注。"""
    try:
        import requests

        def _gateway():
            r = requests.get(_MARKET_OVERVIEW_URL, timeout=6)
            return r.json()

        ov = await asyncio.to_thread(_gateway)
        if ov and not ov.get("error"):
            return {
                "total_main_flow": ov.get("total_main_flow"),  # 亿
                "sh_flow": (ov.get("sh") or {}).get("main_flow"),
                "sz_flow": (ov.get("sz") or {}).get("main_flow"),
                "cyb_flow": (ov.get("cyb") or {}).get("main_flow"),
                "total_amount": ov.get("total_amount"),  # 两市成交额亿
                "up_count": ov.get("up_count"),
                "down_count": ov.get("down_count"),
                "flat_count": ov.get("flat_count"),
                "source": "eastmoney_push2delay_cn",
            }
    except Exception as e:
        logger.debug(f"[报告] 两市资金网关失败: {e}")
    # 兜底: 同花顺行业资金汇总
    try:
        from src.core.marketdata_client import get_market_data

        mcf = await asyncio.to_thread(get_market_data().market_capital_flow)
        if mcf is not None:
            return {
                "total_inflow": mcf.total_inflow,
                "total_outflow": mcf.total_outflow,
                "net_inflow": mcf.net_inflow,
                "board_count": mcf.board_count,
                "source": mcf.source or "ths_hyzjl",
            }
    except Exception as e:
        logger.debug(f"[报告] ths 大盘资金失败: {e}")
    return {"error": "数据获取失败"}


async def _collect_flash_news(limit: int = 15) -> list[dict]:
    """隔夜/当日重要快讯(市场级 7×24)。失败返回空并标注。"""
    try:
        from src.core.marketdata_client import get_market_data

        arts = await asyncio.to_thread(
            get_market_data().flash_news, market="CN", limit=limit
        )
        out = []
        for a in arts:
            try:
                t = a.publish_time.strftime("%m-%d %H:%M")
            except Exception:
                t = ""
            out.append(
                {
                    "time": t,
                    "title": a.title,
                    "source": a.source,
                    "importance": a.importance,
                    "url": a.url,
                    "symbols": list(a.symbols or []),
                }
            )
        return out
    except Exception as e:
        logger.warning(f"[报告] 快讯获取失败: {e}")
        return []


async def _collect_entry_candidates(db, limit: int = 8) -> list[dict]:
    """今日关注候选: entry_candidates 表(最新快照 active, 按 score 降序)。"""
    from sqlalchemy import func as sa_func

    from src.web.models import EntryCandidate

    try:
        latest = (
            db.query(sa_func.max(EntryCandidate.snapshot_date)).scalar()
        )
        if not latest:
            return []
        rows = (
            db.query(EntryCandidate)
            .filter(
                EntryCandidate.snapshot_date == latest,
                EntryCandidate.status == "active",
            )
            .order_by(EntryCandidate.score.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "symbol": c.stock_symbol,
                "name": c.stock_name,
                "action": c.action_label or c.action,
                "score": c.score,
                "signal": c.signal or "",
                "reason": (c.reason or "")[:120],
                "entry_low": c.entry_low,
                "entry_high": c.entry_high,
                "stop_loss": c.stop_loss,
                "target_price": c.target_price,
            }
            for c in rows
        ]
    except Exception as e:
        logger.warning(f"[报告] 关注候选读取失败: {e}")
        return []


async def _collect_positions(db, with_quotes: bool = True) -> list[dict]:
    """用户持仓(positions 表 join stocks) + 实时/前日行情。失败标注。"""
    from src.web.models import Position, Stock

    try:
        rows = (
            db.query(Position, Stock)
            .join(Stock, Position.stock_id == Stock.id)
            .filter(Position.quantity > 0)
            .all()
        )
    except Exception as e:
        logger.warning(f"[报告] 持仓读取失败: {e}")
        return []

    if not rows:
        return []

    # 按市场分组批量拉行情
    sym_by_market: dict[str, list[str]] = {}
    meta = {}
    for pos, st in rows:
        sym_by_market.setdefault(st.market, []).append(st.symbol)
        meta[(st.market, st.symbol)] = (pos, st)

    quote_map = {}
    if with_quotes:
        try:
            from src.core.marketdata_client import md_quote_rows

            for market, syms in sym_by_market.items():
                qs = await asyncio.to_thread(md_quote_rows, syms, market)
                for q in qs:
                    quote_map[(q.get("market", market), q.get("symbol"))] = q
        except Exception as e:
            logger.warning(f"[报告] 持仓行情获取失败: {e}")

    out = []
    for (market, sym), (pos, st) in meta.items():
        q = quote_map.get((market, sym))
        row = {
            "symbol": sym,
            "name": st.name,
            "market": market,
            "cost_price": pos.cost_price,
            "quantity": pos.quantity,
            "trading_style": pos.trading_style or "swing",
        }
        if q:
            row["current_price"] = q.get("current_price")
            row["change_pct"] = q.get("change_pct")
            row["prev_close"] = q.get("prev_close")
        else:
            row["quote_error"] = "数据获取失败"
        # 浮盈(基于成本价)
        if q and pos.cost_price:
            cur = q.get("current_price")
            if cur:
                row["pnl_pct"] = (cur - pos.cost_price) / pos.cost_price * 100
        out.append(row)
    return out


async def _collect_strategy_signals(db, limit: int = 10) -> list[dict]:
    """策略信号回顾: strategy_signal_runs 今日 active 记录, 按 rank_score 降序。"""
    from src.web.models import StrategySignalRun

    try:
        today = beijing_now().strftime("%Y-%m-%d")
        rows = (
            db.query(StrategySignalRun)
            .filter(
                StrategySignalRun.snapshot_date == today,
                StrategySignalRun.status == "active",
            )
            .order_by(StrategySignalRun.rank_score.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "symbol": s.stock_symbol,
                "name": s.stock_name,
                "strategy": s.strategy_name or s.strategy_code,
                "action": s.action_label or s.action,
                "score": s.score,
                "rank_score": s.rank_score,
                "confidence": s.confidence,
                "reason": (s.reason or "")[:120],
                "entry_low": s.entry_low,
                "entry_high": s.entry_high,
                "stop_loss": s.stop_loss,
            }
            for s in rows
        ]
    except Exception as e:
        logger.warning(f"[报告] 策略信号读取失败: {e}")
        return []


async def _collect_data(report_type: str, db) -> dict:
    """按报告类型采集全部数据(每段独立降级, 不互相拖累)。"""
    today = beijing_now().strftime("%Y%m%d")
    data: dict = {
        "generated_at": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
        "market_date": beijing_now().strftime("%Y-%m-%d"),
        "sources": {},
    }

    if report_type == "premarket":
        summary = await _collect_limit_up_summary()
        data["yesterday_limit_up"] = summary if summary.get("limit_up_count") is not None else {"error": "数据获取失败"}
        data["news"] = await _collect_flash_news(15)
        data["candidates"] = await _collect_entry_candidates(db)
        data["positions"] = await _collect_positions(db, with_quotes=True)
        data["sources"]["涨停池"] = "wudao/东财" if not data["yesterday_limit_up"].get("error") else "数据获取失败"
        data["sources"]["快讯"] = "cls/ths" if data["news"] else "数据获取失败"
        data["sources"]["候选/持仓"] = "本地库"
    else:
        data["indices"] = await _collect_indices()
        data["market_flow"] = await _collect_market_flow()
        summary = await _collect_limit_up_summary()
        data["limit_up"] = summary if summary.get("limit_up_count") is not None else {"error": "数据获取失败"}
        data["limit_down"] = await _collect_limit_down(today)
        data["positions"] = await _collect_positions(db, with_quotes=True)
        data["strategy_signals"] = await _collect_strategy_signals(db)
        data["sources"]["指数"] = "腾讯" if data["indices"] else "数据获取失败"
        data["sources"]["资金流"] = data["market_flow"].get("source", "数据获取失败") if not data["market_flow"].get("error") else "数据获取失败"
        data["sources"]["涨停池"] = "wudao/东财" if not data["limit_up"].get("error") else "数据获取失败"
        data["sources"]["跌停"] = "wudao/东财" if data["limit_down"] else "数据获取失败(或无跌停)"
        data["sources"]["信号/持仓"] = "本地库"

    return data


# ──────────────────────────── LLM 生成 ────────────────────────────

_SYSTEM_PROMPT = """你是一名专业的 A 股市场分析师, 为量化交易系统 SIDA 撰写{report_cn}报告。

写作要求(必须严格遵守):
1. 严格基于用户消息中提供的真实数据, 绝不编造任何价格/家数/事件/新闻。
2. 某小节数据标注为「数据获取失败」或缺失时, 报告中对应位置明确写「数据获取失败」, 不得猜测补全或跳过不提。
3. 输出 Markdown, 结构固定:
   - 第一行: `# {title}`(一级标题, 即报告标题)
   - 随后若干数据小节(二级标题 `## `, 用小节罗列真实数据, 可含表格)
   - 一个「市场解读」小节(基于数据做客观分析, 不预测未提供的信息)
   - 一个「风险提示」小节(至少 3 条)
4. 语言: 简体中文, 简洁专业, 直接输出报告正文, 不要输出任何解释或前后缀。
"""


def _build_user_content(report_type: str, data: dict) -> str:
    """把采集数据拼成 LLM 可消费的结构化文本(JSON 序列化 + 摘要)。"""
    import json

    payload = {
        "report_type": report_type,
        "generated_at": data.get("generated_at"),
        "market_date": data.get("market_date"),
    }
    # 只送可序列化字段
    for key in (
        "indices", "market_flow", "yesterday_limit_up", "limit_up",
        "limit_down", "news", "candidates", "positions", "strategy_signals",
        "sources",
    ):
        if key in data:
            payload[key] = data[key]
    return json.dumps(payload, ensure_ascii=False, indent=1, default=str)


async def _llm_generate(db, report_type: str, data: dict) -> str | None:
    """经统一 LLM 配置中心(场景 reports)调用生成报告。失败返回 None。"""
    try:
        from src.core.ai_client import AIClient, get_model_for_scene
        from src.web.models import AIService

        model = get_model_for_scene(db, "reports")
        if model is None:
            logger.info("[报告] 模型池为空, 使用模板报告")
            return None
        service = (
            db.query(AIService).filter(AIService.id == model.service_id).first()
        )
        if service is None or not service.base_url or not service.api_key:
            logger.warning("[报告] 服务商未配置(base_url/api_key), 使用模板报告")
            return None

        client = AIClient(
            base_url=service.base_url,
            api_key=service.api_key,
            model=model.model,
        )
        system_prompt = _SYSTEM_PROMPT.format(
            report_cn="盘前" if report_type == "premarket" else "盘后",
            title=REPORT_TITLES[report_type],
        )
        user_content = _build_user_content(report_type, data)
        content = await client.chat(system_prompt, user_content, temperature=0.4)
        content = (content or "").strip()
        if not content:
            logger.warning("[报告] LLM 返回空内容, 使用模板报告")
            return None
        logger.info("[报告] LLM 生成成功 model=%s", model.model)
        return content
    except Exception as e:
        logger.warning(f"[报告] LLM 生成失败, 降级模板: {e}")
        return None


# ──────────────────────────── 模板报告(纯数据, 不编造) ────────────────────────────


def _fmt_flow(v) -> str:
    if v is None:
        return "数据获取失败"
    return f"{v:+.1f}亿" if isinstance(v, (int, float)) else str(v)


def _build_template_report(report_type: str, data: dict) -> str:
    """LLM 不可用时的纯数据报告: 只罗列真实采集到的数据。"""
    lines = [f"# {REPORT_TITLES[report_type]} {data.get('market_date', '')}", ""]
    lines.append(f"**生成时间:** {data.get('generated_at', '')}")
    lines.append("**生成方式:** 数据模板(LLM 不可用, 纯数据不解读)")
    lines.append("")

    if report_type == "premarket":
        lines.append("## 一、昨日市场回顾(涨停复盘)")
        ylu = data.get("yesterday_limit_up") or {}
        if ylu.get("error"):
            lines.append(f"- 涨停家数: {ylu.get('error')}")
        else:
            lines.append(f"- 涨停家数: {ylu.get('limit_up_count')} 家, 最高连板: {ylu.get('max_streak')} 板")
            ladder = ylu.get("ladder") or {}
            if ladder:
                lines.append("- 连板梯队: " + ", ".join(f"{k}板×{v}" for k, v in sorted(ladder.items(), reverse=True)))
            if ylu.get("top_stocks"):
                lines.append("- 高位股: " + ", ".join(str(s) for s in ylu["top_stocks"]))
            if ylu.get("top_sectors"):
                lines.append("- 涨停集中板块: " + ", ".join(f"{s['name']}×{s['count']}" for s in ylu["top_sectors"]))
        lines.append("")

        lines.append("## 二、隔夜重要消息")
        news = data.get("news") or []
        if not news:
            lines.append("- 数据获取失败(或暂无快讯)")
        else:
            for n in news[:10]:
                star = "⭐" * (n.get("importance") or 0)
                lines.append(f"- [{n.get('time', '')}] {star}{n.get('title', '')}({n.get('source', '')})")
        lines.append("")

        lines.append("## 三、今日关注候选")
        cands = data.get("candidates") or []
        if not cands:
            lines.append("- 暂无候选(entry_candidates 表为空)")
        else:
            for c in cands:
                entry = ""
                if c.get("entry_low") is not None and c.get("entry_high") is not None:
                    entry = f" 区间[{c['entry_low']:.2f},{c['entry_high']:.2f}]"
                lines.append(
                    f"- {c.get('name', '')}({c.get('symbol', '')}) 动作={c.get('action', '')} "
                    f"分={c.get('score', 0):.1f}{entry}"
                    + (f" 理由: {c.get('reason', '')}" if c.get("reason") else "")
                )
        lines.append("")

        lines.append("## 四、当前持仓(参考前收盘)")
        pos = data.get("positions") or []
        if not pos:
            lines.append("- 暂无持仓数据")
        else:
            for p in pos:
                if p.get("quote_error"):
                    cur = f"行情: {p['quote_error']}"
                else:
                    cur = (
                        f"现价 {p.get('current_price', '-')} "
                        f"({p.get('change_pct', 0):+.2f}%) 前收 {p.get('prev_close', '-')}"
                    )
                lines.append(
                    f"- {p.get('name', '')}({p.get('symbol', '')}) "
                    f"{p.get('quantity', 0)}股 成本 {p.get('cost_price', '-')} {cur}"
                )
        lines.append("")

        lines.append("## 五、风险提示")
        lines.append("- 本报告由 SIDA 自动生成, 数据源可能存在延迟或缺失, 仅供参考。")
        lines.append("- 涨停/情绪数据为最近交易日口径, 竞价情况以开盘后实际为准。")
        lines.append("- 不构成投资建议, 入市需谨慎。")
    else:
        lines.append("## 一、今日大盘")
        idx = data.get("indices") or []
        if not idx:
            lines.append("- 指数数据获取失败")
        else:
            for i in idx:
                pct = i.get("change_pct")
                pct_s = f"{pct:+.2f}%" if isinstance(pct, (int, float)) else "数据获取失败"
                lines.append(f"- {i.get('name', '')}: {i.get('price', '-')} ({pct_s})")
        lines.append("")

        lines.append("## 二、两市资金流")
        mf = data.get("market_flow") or {}
        if mf.get("error"):
            lines.append(f"- 资金流: {mf.get('error')}")
        elif "total_main_flow" in mf:
            lines.append(f"- 两市主力净流入: {_fmt_flow(mf.get('total_main_flow'))}")
            lines.append(f"- 沪市主力: {_fmt_flow(mf.get('sh_flow'))} / 深市主力: {_fmt_flow(mf.get('sz_flow'))}")
            if mf.get("total_amount") is not None:
                lines.append(f"- 两市成交额: {mf.get('total_amount')} 亿")
            if mf.get("up_count") is not None:
                lines.append(f"- 涨跌家数: 涨 {mf.get('up_count')} / 跌 {mf.get('down_count')} / 平 {mf.get('flat_count')}")
        else:
            lines.append(
                f"- 行业资金净额: {_fmt_flow(mf.get('net_inflow'))} "
                f"(流入 {_fmt_flow(mf.get('total_inflow'))} / 流出 {_fmt_flow(mf.get('total_outflow'))}, "
                f"来源 {mf.get('source', '')})"
            )
        lines.append("")

        lines.append("## 三、涨停复盘")
        lu = data.get("limit_up") or {}
        if lu.get("error"):
            lines.append(f"- 涨停数据: {lu.get('error')}")
        else:
            lines.append(f"- 涨停家数: {lu.get('limit_up_count')} 家, 最高连板: {lu.get('max_streak')} 板")
            ladder = lu.get("ladder") or {}
            if ladder:
                lines.append("- 连板梯队: " + ", ".join(f"{k}板×{v}" for k, v in sorted(ladder.items(), reverse=True)))
            if lu.get("top_stocks"):
                lines.append("- 高位股: " + ", ".join(str(s) for s in lu["top_stocks"]))
            if lu.get("top_sectors"):
                lines.append("- 涨停集中板块: " + ", ".join(f"{s['name']}×{s['count']}" for s in lu["top_sectors"]))
        ld = data.get("limit_down") or []
        if ld:
            lines.append("- 跌停: " + ", ".join(f"{d.get('name', '')}({d.get('code', '')})" for d in ld[:15]))
        else:
            lines.append("- 跌停: 数据获取失败(或当日无跌停)")
        lines.append("")

        lines.append("## 四、持仓今日表现")
        pos = data.get("positions") or []
        if not pos:
            lines.append("- 暂无持仓数据")
        else:
            for p in pos:
                if p.get("quote_error"):
                    cur = f"行情: {p['quote_error']}"
                else:
                    pnl = p.get("pnl_pct")
                    pnl_s = f" 浮盈 {pnl:+.2f}%" if pnl is not None else ""
                    cur = f"现价 {p.get('current_price', '-')} ({p.get('change_pct', 0):+.2f}%){pnl_s}"
                lines.append(
                    f"- {p.get('name', '')}({p.get('symbol', '')}) "
                    f"{p.get('quantity', 0)}股 成本 {p.get('cost_price', '-')} {cur}"
                )
        lines.append("")

        lines.append("## 五、策略信号回顾")
        sigs = data.get("strategy_signals") or []
        if not sigs:
            lines.append("- 今日无策略信号记录(strategy_signal_runs 为空)")
        else:
            for s in sigs:
                lines.append(
                    f"- {s.get('name', '')}({s.get('symbol', '')}) "
                    f"[{s.get('strategy', '')}] 动作={s.get('action', '')} "
                    f"评分={s.get('score', 0):.1f}"
                    + (f" 理由: {s.get('reason', '')}" if s.get("reason") else "")
                )
        lines.append("")

        lines.append("## 六、风险提示")
        lines.append("- 本报告由 SIDA 自动生成, 数据源可能存在延迟或缺失, 仅供参考。")
        lines.append("- 资金流/涨停数据为收盘口径, 盘中或有变化。")
        lines.append("- 不构成投资建议, 入市需谨慎。")

    return "\n".join(lines)


# ──────────────────────────── 写盘 ────────────────────────────


def _write_report(job_id: str, title: str, content: str, run_time: datetime) -> dict:
    """写入 CRON_OUTPUT_DIR/<job_id>/YYYY-MM-DD_HH-MM-SS.md, 返回 {path, title, size}。"""
    job_dir = _report_root() / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    fname = run_time.strftime("%Y-%m-%d_%H-%M-%S") + ".md"
    path = job_dir / fname
    body = content.strip() + "\n"
    path.write_text(body, encoding="utf-8")
    size = path.stat().st_size
    logger.info("[报告] 已写入 %s (%d bytes)", path, size)
    return {"path": str(path), "title": title, "size": size}


# ──────────────────────────── 入口 ────────────────────────────


async def generate_market_report(report_type: str, db=None) -> dict:
    """生成盘前(premarket)/盘后(postmarket)报告并写入报告中心。

    Args:
        report_type: "premarket" 或 "postmarket"
        db: 可选 SQLAlchemy session; 不传则内部自开自关。

    Returns:
        {"path": str, "title": str, "size": int}
    """
    if report_type not in JOB_IDS:
        raise ValueError(f"未知报告类型: {report_type} (支持 premarket/postmarket)")

    own_db = db is None
    if own_db:
        db = _db_session()
    try:
        data = await _collect_data(report_type, db)
        content = await _llm_generate(db, report_type, data)
        if content is None:
            content = _build_template_report(report_type, data)

        run_time = beijing_now()
        job_id = JOB_IDS[report_type]
        title = f"# {REPORT_TITLES[report_type]} {run_time.strftime('%Y-%m-%d')}"
        # 带 title 元信息行(Hermes cron 报告同款格式: 首行标题 + 元信息块)
        header = (
            f"{title}\n\n"
            f"**Job ID:** {job_id}\n"
            f"**Run Time:** {run_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"**Generated By:** SIDA 内置报告生成器\n"
            f"**数据源:** {', '.join(f'{k}={v}' for k, v in (data.get('sources') or {}).items()) or '未知'}\n\n"
        )
        if content.startswith("# "):
            # LLM/模板已自带标题 → 去掉其首行避免重复, 保留元信息块
            body_lines = content.split("\n", 1)
            content = body_lines[1].strip() if len(body_lines) > 1 else ""
            content = header + content
        else:
            content = header + content

        return _write_report(job_id, title, content, run_time)
    finally:
        if own_db:
            try:
                db.close()
            except Exception:
                pass
