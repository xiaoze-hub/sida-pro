"""标的全面证据档案聚合器 (Stock Full Dossier Aggregator)

作为 SIDA-Pro AI 助手的核心 Harness（编排线束），
负责在分析单只股票时，以毫秒级并行并发方式打包平台的全部维度客观证据：

1. 【实时量价盘口】现价、涨跌幅、量比、振幅、均线多空偏离度 (get_stock_quote + get_technical_analysis)
2. 【K线形态】同花顺 29 形态教学体系 + TA-Lib 标准形态（基于日K）
3. 【主力真金意图】腾讯逐笔大单吸筹/派发、内外盘博弈 (get_main_intent)
4. 【底仓筹码沉淀】120日自算CYQ成本峰、70%/90%筹码带、获利盘 (compute_main_accumulation_chips)
5. 【数智决策三指标】GS多空色带、机构活跃度生命线、L2主力净流 (get_decision_pioneer)
6. 【机构研报与共识】东方财富官方研报中心最新券商评级、目标价 (fetch_eastmoney_research_reports)
7. 【近期公告】近 15 日公司公告标题与类型（东财公告源，权威一手信息）
8. 【利好面消息】近 72 小时个股新闻，规则打分筛出利好条目（含命中词，来源可溯）
9. 【利空/风险消息】同窗口反向验证，防止单边叙事

避免大模型单轮 ReAct 盲探、丢三落四或口径混淆。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.orm import Session

from src.db.models import User

logger = logging.getLogger(__name__)

# ── 利好/利空规则词典（规则打分，非 LLM 推测；只做筛选排序，不改写原文） ──
_POSITIVE_KEYWORDS: tuple[str, ...] = (
    # 订单 / 合同 / 中标
    "中标", "预中标", "签订", "签署", "合同", "订单", "框架协议", "战略合作", "合作协议",
    # 业绩
    "业绩预增", "预增", "扭亏", "净利润增长", "营收增长", "超预期", "创历史新高", "毛利率提升", "同比增长",
    # 股东行为
    "增持", "回购", "定增获批", "股权激励", "员工持股",
    # 分红送转
    "分红", "派息", "送转", "高送转", "特别股息",
    # 产品 / 产能 / 技术
    "涨价", "提价", "调价", "量产", "投产", "扩产", "产能释放", "新产品", "新品发布",
    "技术突破", "专利", "获批", "通过认证", "进入供应", "定点", "首台套", "供货",
    # 资本运作
    "收购", "并购", "重组", "资产注入", "借壳", "引入战投", "国资入股", "战略投资", "实控人变更",
    # 政策 / 补贴
    "补贴", "税收优惠", "政策支持", "试点", "纳入目录",
)

_NEGATIVE_KEYWORDS: tuple[str, ...] = (
    "减持", "清仓", "质押", "冻结", "预亏", "亏损", "业绩下滑", "净利润下降", "商誉减值", "计提",
    "立案", "调查", "处罚", "罚款", "违规", "警示函", "问询函", "关注函", "监管函", "谴责",
    "诉讼", "仲裁", "败诉", "退市", "暂停上市", "终止", "失败", "解禁", "风险提示", "更正",
)


def classify_news_sentiment(title: str, content: str = "") -> tuple[str, int, list[str]]:
    """规则打分判定消息倾向，返回 (倾向, 分数, 命中词)。

    倾向 ∈ {利好, 利空, 中性}；分数 = 正面词命中数 - 负面词命中数。
    只做客观筛选排序，不改写原文，不做主观推断。命中词随结果返回便于溯源。
    """
    text = f"{title or ''} {content or ''}"[:600]
    pos_hits = [kw for kw in _POSITIVE_KEYWORDS if kw in text]
    neg_hits = [kw for kw in _NEGATIVE_KEYWORDS if kw in text]
    score = len(pos_hits) - len(neg_hits)
    if score > 0:
        return "利好", score, pos_hits
    if score < 0:
        return "利空", score, neg_hits
    return "中性", 0, []


def _clean_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    return s.split(".")[0] if "." in s else s


def _fmt_dt(v: Any) -> str:
    try:
        return v.strftime("%Y-%m-%d %H:%M") if hasattr(v, "strftime") else str(v or "")
    except Exception:  # noqa: BLE001
        return ""


async def _fetch_announcements(sym: str, market: str, days: int = 15) -> list[dict[str, Any]]:
    """近 N 日公司公告（东财公告源；权威一手信息，只取标题+类型+时间+链接）。"""
    try:
        from src.core.marketdata_client import get_market_data

        items = await asyncio.to_thread(
            lambda: get_market_data().events([sym], market=market, since_days=days)
        )
        out: list[dict[str, Any]] = []
        for it in items or []:
            out.append({
                "title": (getattr(it, "title", "") or "").strip(),
                "event_type": (getattr(it, "event_type", "") or "").strip(),
                "publish_time": getattr(it, "publish_time", None),
                "importance": getattr(it, "importance", 0) or 0,
                "url": (getattr(it, "url", "") or "").strip(),
            })
        out.sort(key=lambda x: x.get("publish_time") or 0, reverse=True)
        return out[:30]
    except Exception as e:  # noqa: BLE001
        logger.debug("公告获取异常 %s: %s", sym, e)
        return []


async def _fetch_news_by_sentiment(
    sym: str, market: str, want: str, hours: int = 72, limit: int = 8, stock_name: str = ""
) -> list[dict[str, Any]]:
    """近 N 小时个股新闻，先剔除全市场泛新闻，再规则打分保留指定倾向条目。

    相关性过滤：item.symbols 含本股代码，或标题出现本股名称。东财个股新闻常把
    全市场汇总稿一并返回（如「168家公司预告前三季度业绩」），不过滤会污染结论。
    """
    try:
        from src.core.marketdata_client import get_market_data

        # 未显式传名称时自行解析，避免单独调用本函数时退化成只按 symbols 判定（会串入全市场汇总稿）
        if not stock_name and (market or "CN").upper() == "CN":
            stock_name = await asyncio.to_thread(_resolve_stock_name, sym)

        items = await asyncio.to_thread(
            lambda: get_market_data().news([sym], market=market, since_hours=hours)
        )
        # 上游 since_hours 不是严格时间切分（实测会夹带数月前的旧公告），这里自己做时间下限
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz

        cutoff = _dt.now(_tz.utc) - _td(hours=hours)
        out: list[dict[str, Any]] = []
        for it in items or []:
            title = (getattr(it, "title", "") or "").strip()
            content = (getattr(it, "content", "") or "").strip()
            ts = getattr(it, "publish_time", None)
            if ts is not None and hasattr(ts, "tzinfo"):
                ts_cmp = ts if ts.tzinfo else ts.replace(tzinfo=_tz.utc)
                if ts_cmp < cutoff:
                    continue
            # 相关性过滤：以标题是否点名本股为准（宁可漏，不可把别家新闻算到本股头上）。
            # 东财个股新闻源会把全市场汇总稿一并打上本股 tag（如「55家公司前三季度业绩预增」），
            # 这类稿件标题不含本股名称，必须剔除；名称缺失时退回 symbols 判定。
            if stock_name:
                related = stock_name in title or sym in title
            else:
                related = sym in (getattr(it, "symbols", None) or [])
            if not related:
                continue
            label, score, hits = classify_news_sentiment(title, content)
            if label != want:
                continue
            out.append({
                "title": title,
                "source": (getattr(it, "source", "") or "").strip(),
                "publish_time": getattr(it, "publish_time", None),
                "score": abs(score),
                "hits": hits,
                "url": (getattr(it, "url", "") or "").strip(),
            })
        out.sort(key=lambda x: (x.get("score", 0), x.get("publish_time") or 0), reverse=True)
        return out[:limit]
    except Exception as e:  # noqa: BLE001
        logger.debug("%s消息获取异常 %s: %s", want, sym, e)
        return []


_name_cache: dict[str, str] = {}


def _resolve_stock_name(sym: str) -> str:
    """代码 → 名称（本地股票列表优先，通达信 name_map 兜底；失败返回空串）。"""
    if not sym:
        return ""
    if sym in _name_cache:
        return _name_cache[sym]
    name = ""
    try:
        from src.collectors.stock_list import get_stock_list

        for s in get_stock_list() or []:
            if str(s.get("symbol") or "").strip() == sym:
                name = str(s.get("name") or "").strip()
                break
    except Exception as e:  # noqa: BLE001
        logger.debug("股票名称解析失败(stock_list) %s: %s", sym, e)
    if not name:
        try:
            from src.core.tdx_boards import name_map

            name = (name_map() or {}).get(sym, "") or ""
        except Exception as e:  # noqa: BLE001
            logger.debug("股票名称解析失败(tdx) %s: %s", sym, e)
    if name:
        _name_cache[sym] = name
    return name


async def _fetch_kline_patterns(sym: str, market: str) -> str:
    """K 线形态识别（同花顺 29 形态体系 + TA-Lib 标准形态）。"""
    try:
        from src.web.api.chat import _fetch_kline_pattern_context

        return await _fetch_kline_pattern_context(sym, market)
    except Exception as e:  # noqa: BLE001
        logger.debug("K线形态识别异常 %s: %s", sym, e)
        return f"K线形态识别失败: {e}"


async def build_stock_full_dossier(
    symbol: str,
    market: str = "CN",
    db: Session | None = None,
    user: User | None = None,
) -> dict[str, Any]:
    """并发收集单只标的的全景证据档案（9 维）。"""
    clean_sym = _clean_symbol(symbol)
    mkt = (market or "CN").upper()

    dossier: dict[str, Any] = {
        "symbol": clean_sym,
        "market": mkt,
        "quote": None,
        "technical": None,
        "kline_patterns": "",
        "main_intent": None,
        "chips": None,
        "decision_pioneer": None,
        "broker_reports": [],
        "announcements": [],
        "positive_news": [],
        "negative_news": [],
        "errors": {},
    }

    async def _fetch_quote():
        try:
            from src.agents.chat.registry import _tool_get_stock_quote

            return await _tool_get_stock_quote(db, {"symbol": clean_sym, "market": mkt}, user=user)
        except Exception as e:  # noqa: BLE001
            return f"行情获取异常: {e}"

    async def _fetch_technical():
        try:
            from src.agents.chat.registry import _tool_get_technical_analysis

            return await _tool_get_technical_analysis(db, {"symbol": clean_sym, "market": mkt}, user=user)
        except Exception as e:  # noqa: BLE001
            return f"技术分析异常: {e}"

    async def _fetch_intent():
        try:
            from src.agents.chat.registry import _tool_get_main_intent

            return await _tool_get_main_intent(db, {"symbol": clean_sym, "market": mkt}, user=user)
        except Exception as e:  # noqa: BLE001
            return f"主力意图异常: {e}"

    async def _fetch_chips():
        try:
            from src.agents.chat.tools_market import _tool_get_chip_accumulation

            return await _tool_get_chip_accumulation(db, {"symbol": clean_sym, "market": mkt}, user=user)
        except Exception as e:  # noqa: BLE001
            return f"筹码沉淀异常: {e}"

    async def _fetch_decision():
        try:
            from src.agents.chat.registry import _tool_get_decision_pioneer

            return await _tool_get_decision_pioneer(db, {"symbol": clean_sym, "market": mkt}, user=user)
        except Exception as e:  # noqa: BLE001
            return f"数智决策三指标异常: {e}"

    async def _fetch_reports():
        try:
            from src.collectors.eastmoney_research import fetch_eastmoney_research_reports

            res = await asyncio.to_thread(fetch_eastmoney_research_reports, clean_sym, page=1, page_size=5)
            return res.get("items") or []
        except Exception as e:  # noqa: BLE001
            logger.debug("研报获取异常: %s", e)
            return []

    async def _empty():
        return []

    # 9 维并行调度（公告/消息面仅 A 股走东财源，非 A 股直接空数组）
    is_cn = mkt == "CN"
    stock_name = await asyncio.to_thread(_resolve_stock_name, clean_sym) if is_cn else ""
    (
        quote_res,
        tech_res,
        pattern_res,
        intent_res,
        chips_res,
        dec_res,
        reports_res,
        ann_res,
        pos_res,
        neg_res,
    ) = await asyncio.gather(
        _fetch_quote(),
        _fetch_technical(),
        _fetch_kline_patterns(clean_sym, mkt),
        _fetch_intent(),
        _fetch_chips(),
        _fetch_decision(),
        _fetch_reports(),
        _fetch_announcements(clean_sym, mkt) if is_cn else _empty(),
        _fetch_news_by_sentiment(clean_sym, mkt, "利好", stock_name=stock_name) if is_cn else _empty(),
        _fetch_news_by_sentiment(clean_sym, mkt, "利空", limit=5, stock_name=stock_name) if is_cn else _empty(),
        return_exceptions=True,
    )

    def _txt(v: Any) -> str:
        if isinstance(v, str):
            return v
        return f"获取失败: {v}"

    dossier["quote"] = _txt(quote_res)
    dossier["technical"] = _txt(tech_res)
    dossier["kline_patterns"] = _txt(pattern_res)
    dossier["main_intent"] = _txt(intent_res)
    dossier["chips"] = _txt(chips_res)
    dossier["decision_pioneer"] = _txt(dec_res)
    dossier["broker_reports"] = reports_res if isinstance(reports_res, list) else []
    dossier["announcements"] = ann_res if isinstance(ann_res, list) else []
    dossier["positive_news"] = pos_res if isinstance(pos_res, list) else []
    dossier["negative_news"] = neg_res if isinstance(neg_res, list) else []

    return dossier


def format_stock_dossier_text(dossier: dict[str, Any]) -> str:
    """将全景档案转换为清晰、无歧义的 AI 分析线束上下文。"""
    sym = dossier.get("symbol", "")
    lines = [
        f"=================== 【SIDA-Pro 标的客观全景档案 · {sym}】 ===================",
        "⚠️ 本档案已由系统 Harness 统一并行采集完毕，含盘面/形态/逐笔主力/底仓筹码/决策色带/机构研报/公告/利好面 共 9 维证据。",
        "⚠️ 公告与消息均为原文标题摘录，未改写；缺失一律标注「暂无数据」，不得推测补全。",
        "",
        "【第一维：实时行情与盘口态势】",
        dossier.get("quote") or "暂无行情",
        "",
        "【第二维：均线与技术面状态】",
        dossier.get("technical") or "暂无技术面",
        "",
        "【第三维：K线形态识别（同花顺29形态 + TA-Lib）】",
        dossier.get("kline_patterns") or "暂无形态数据",
        "",
        "【第四维：主力意图与真金证据（腾讯逐笔主动口径）】",
        dossier.get("main_intent") or "暂无主力意图",
        "",
        "【第五维：底仓筹码沉淀与获利带（自算CYQ·120日模型）】",
        dossier.get("chips") or "暂无筹码数据",
        "",
        "【第六维：数智决策三指标（GS多空色带 + 机构活跃度 + TQ暗盘）】",
        dossier.get("decision_pioneer") or "暂无决策三指标",
        "",
    ]

    reports = dossier.get("broker_reports") or []
    lines.append("【第七维：券商机构研报（东方财富官方研报中心最新收录）】")
    if reports:
        for r in reports[:3]:
            lines.append(
                f"- [{r.get('publish_date')}] 《{r.get('title')}》 | 机构: {r.get('org_name')} | "
                f"评级: 【{r.get('rating')}】 | 分析师: {r.get('author') or '详见研报'}"
            )
    else:
        lines.append("- 近期暂无公开券商深度研报")
    lines.append("")

    lines.append("【第八维：近期公司公告（近 15 日，东财公告源·原文标题）】")
    anns = dossier.get("announcements") or []
    if anns:
        for a in anns[:12]:
            et = f"[{a.get('event_type')}]" if a.get("event_type") else ""
            imp = "⭐" if (a.get("importance") or 0) >= 2 else ""
            lines.append(f"- {_fmt_dt(a.get('publish_time'))[:10]} {imp}{et} {a.get('title')}")
    else:
        lines.append("- 近 15 日暂无公司公告")
    lines.append("")

    lines.append("【第九维：利好面消息（近 72 小时个股新闻·规则筛选，含命中词，来源可溯）】")
    pos = dossier.get("positive_news") or []
    if pos:
        for n in pos[:6]:
            hits = "/".join((n.get("hits") or [])[:3])
            lines.append(
                f"- {_fmt_dt(n.get('publish_time'))[:16]} {n.get('title')}"
                f"（命中: {hits or '—'}｜来源: {n.get('source') or '—'}）"
            )
    else:
        lines.append("- 近 72 小时暂无规则筛出的利好条目（仅代表未命中利好词，不代表无消息）")
    lines.append("")

    lines.append("【第十维：利空/风险消息（反向验证，近 72 小时）】")
    neg = dossier.get("negative_news") or []
    if neg:
        for n in neg[:5]:
            hits = "/".join((n.get("hits") or [])[:3])
            lines.append(f"- {_fmt_dt(n.get('publish_time'))[:16]} {n.get('title')}（命中: {hits or '—'}）")
    else:
        lines.append("- 近 72 小时暂无规则筛出的利空条目")
    lines.append("==========================================================================")

    return "\n".join(lines)
