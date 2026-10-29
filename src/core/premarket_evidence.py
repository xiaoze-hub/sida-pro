"""盘前结构化证据层 (src/core/premarket_evidence.py)。

职责：
- 统一采集盘前六大证据块：`overnight` / `ladder` / `news` / `sentiment` / `watchlist` / `portfolio`
- 每块结构固定：`{"items": [...], "quality": "ok|suspect|missing", "reason": str, "source": str, "as_of": iso}`
- 在取数层完成失真判定（不把脏数据判断丢给 LLM）：
  1) 涨停家数 > 全市场股票数上限（或为负数）→ `suspect`
  2) 连板高度与梯队家数矛盾（如 max_streak 与 ladder 最大板数不一致、梯队家数之和与总涨停数矛盾等）→ `suspect`
  3) 快讯全部为同一时间戳（≥2 条且时间戳完全重复或全空）→ `suspect`
- 纯取数：不写文件、不调 LLM、不发推送；单块异常独立降级为 `missing` + `reason`，绝不向外抛异常。
- 严禁依赖 `src/web`。
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Any, Literal, TypedDict

from src.core.timezone import beijing_now

logger = logging.getLogger(__name__)

QualityStatus = Literal["ok", "suspect", "missing"]

# A 股全市场股票总数合理上限（超过即判定为数据源失真，如 5580 家异常）
MAX_MARKET_STOCKS = 5400
# 连板数合理上限
MAX_REASONABLE_STREAK = 30

# 快讯主题关键词映射（纯规则聚类，不调 LLM）
_NEWS_THEME_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("算力/AI", ("算力", "人工智能", "大模型", "芯片", "半导体", "智算", "GPU", "光模块")),
    ("低空/商业航天", ("低空", "eVTOL", "飞行汽车", "商业航天", "卫星", "火箭", "无人机")),
    ("机器人/智造", ("机器人", "人形", "减速器", "具身智能", "工业母机", "自动化")),
    ("新能源/电力", ("光伏", "锂电", "储能", "风电", "电网", "特高压", "新能源", "固态电池")),
    ("大宗/涨价", ("涨价", "提价", "停产", "限产", "减产", "黄金", "原油", "铜", "稀土", "化工")),
    ("宏观/政策", ("央行", "降准", "降息", "财政", "证监会", "国务院", "发改委", "美联储", "GDP", "CPI")),
    ("医药/消费", ("创新药", "医疗", "集采", "白酒", "消费", "旅游", "地产", "房价")),
)


class EvidenceBlock(TypedDict):
    items: list[dict[str, Any]]
    quality: QualityStatus
    reason: str
    source: str
    as_of: str


class PremarketEvidence(TypedDict):
    date: str
    as_of: str
    user_id: str | None
    overnight: EvidenceBlock
    ladder: EvidenceBlock
    news: EvidenceBlock
    sentiment: EvidenceBlock
    watchlist: EvidenceBlock
    portfolio: EvidenceBlock
    fund_structure: EvidenceBlock


def _now_iso() -> str:
    return beijing_now().isoformat(timespec="seconds")


def _normalize_date(date_str: str | None) -> tuple[str, str]:
    """返回 (YYYY-MM-DD, YYYYMMDD)。"""
    if not date_str:
        now = beijing_now()
        return now.strftime("%Y-%m-%d"), now.strftime("%Y%m%d")
    s = str(date_str).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}", s
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        d10 = s[:10]
        return d10, d10.replace("-", "")
    now = beijing_now()
    return now.strftime("%Y-%m-%d"), now.strftime("%Y%m%d")


def _make_block(
    items: list[dict[str, Any]],
    quality: QualityStatus,
    reason: str = "",
    source: str = "",
    as_of: str | None = None,
) -> EvidenceBlock:
    return {
        "items": items,
        "quality": quality,
        "reason": reason,
        "source": source,
        "as_of": as_of or _now_iso(),
    }


# ──────────────────────────── 失真判定（纯函数，单测友好） ────────────────────────────


def audit_ladder_summary(summary: dict[str, Any] | None) -> tuple[QualityStatus, str]:
    """对涨停/连板梯队摘要执行确定性失真审计。

    返回 (quality, reason)：
    - missing: summary 为空、含 error 或无 limit_up_count
    - suspect:
      1) 涨停家数 < 0 或 > MAX_MARKET_STOCKS
      2) 连板高度与涨停家数矛盾（如涨停家数为 0 但最高板 > 0，或涨停家数 > 0 但最高板 <= 0）
      3) 连板梯队字典最大连板数与 max_streak 不一致，或梯队各档家数之和与涨停总家数矛盾
      4) 连板高度超过合理上限 MAX_REASONABLE_STREAK 或梯队含非正数
    - ok: 通过全部校验
    """
    if not summary or not isinstance(summary, dict):
        return "missing", "无涨停池数据"
    if summary.get("error"):
        return "missing", str(summary.get("error") or "数据获取失败")
    if summary.get("limit_up_count") is None:
        return "missing", "涨停家数缺失"

    try:
        total = int(summary["limit_up_count"])
    except (TypeError, ValueError):
        return "suspect", f"涨停家数格式非法: {summary.get('limit_up_count')!r}"

    if total < 0:
        return "suspect", f"涨停家数不能为负数({total})"
    if total > MAX_MARKET_STOCKS:
        return "suspect", f"涨停家数({total})超过全市场股票数上限({MAX_MARKET_STOCKS})"

    raw_max_streak = summary.get("max_streak", 0)
    try:
        max_streak = int(raw_max_streak or 0)
    except (TypeError, ValueError):
        return "suspect", f"最高连板数格式非法: {raw_max_streak!r}"

    if max_streak < 0 or max_streak > MAX_REASONABLE_STREAK:
        return "suspect", f"连板高度异常({max_streak})"

    if total == 0 and max_streak > 0:
        return "suspect", f"涨停家数为0但最高连板为{max_streak}板(连板高度与家数矛盾)"
    if total > 0 and max_streak <= 0:
        return "suspect", f"涨停家数为{total}但最高连板为{max_streak}板(连板高度与家数矛盾)"

    ladder = summary.get("ladder")
    if isinstance(ladder, dict) and ladder:
        parsed_ladder: dict[int, int] = {}
        for k, v in ladder.items():
            try:
                ik = int(k)
                iv = int(v)
            except (TypeError, ValueError):
                return "suspect", f"连板梯队键值格式非法: {k!r}={v!r}"
            if ik <= 0 or iv <= 0:
                return "suspect", f"连板梯队存在非正板数或家数: {ik}板×{iv}"
            parsed_ladder[ik] = iv

        ladder_max = max(parsed_ladder.keys())
        ladder_sum = sum(parsed_ladder.values())
        if ladder_max != max_streak:
            return (
                "suspect",
                f"连板高度({max_streak}板)与梯队最高板({ladder_max}板)不一致",
            )
        if ladder_sum != total:
            return (
                "suspect",
                f"连板梯队家数合计({ladder_sum})与涨停总家数({total})矛盾",
            )
        max_tier_count = parsed_ladder.get(max_streak, 0)
        if max_tier_count > total:
            return (
                "suspect",
                f"最高连板({max_streak}板)家数({max_tier_count})超过涨停总家数({total})",
            )
    elif total > 0 and isinstance(ladder, dict) and not ladder:
        return "suspect", f"涨停家数为{total}但连板梯队为空(连板高度与家数矛盾)"

    return "ok", ""


def audit_news_items(items: list[dict[str, Any]]) -> tuple[QualityStatus, str]:
    """对快讯列表执行失真审计。

    - 空列表 → missing
    - ≥2 条快讯且所有时间戳完全相同（或全部为空）→ suspect
    - 其余 → ok
    """
    if not items:
        return "missing", "快讯列表为空"

    times = [str(it.get("time") or "").strip() for it in items if isinstance(it, dict)]
    non_empty_times = [t for t in times if t]

    if len(items) >= 2:
        if not non_empty_times:
            return "suspect", "快讯时间戳全部为空"
        if len(set(non_empty_times)) == 1:
            return "suspect", f"快讯全部为同一时间戳({non_empty_times[0]})"

    return "ok", ""


def _classify_news_theme(title: str, symbols: list[str] | None = None) -> str:
    text = title or ""
    for theme, keywords in _NEWS_THEME_KEYWORDS:
        if any(kw in text for kw in keywords):
            return theme
    if symbols:
        return "个股异动"
    return "市场综合"


def enrich_news_clusters(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """为快讯补充主题聚类 (`theme`) 与热度 (`heat`)，同时保留原有字段。"""
    if not raw_items:
        return []
    themes = [
        _classify_news_theme(str(it.get("title") or ""), it.get("symbols"))
        for it in raw_items
    ]
    theme_counts = Counter(themes)
    enriched: list[dict[str, Any]] = []
    for it, theme in zip(raw_items, themes):
        row = dict(it)
        row.setdefault("theme", theme)
        imp = int(row.get("importance") or 0)
        row.setdefault("heat", theme_counts[theme] + imp)
        enriched.append(row)
    return enriched


# ──────────────────────────── 各块采集实现 ────────────────────────────


def _collect_overnight_block(as_of: str) -> EvidenceBlock:
    """采集隔夜外盘（美股指数/全球及亚太指数/大宗商品/汇率）。"""
    items: list[dict[str, Any]] = []
    sources: list[str] = []
    errors: list[str] = []

    # 1. 腾讯美股三大指数
    try:
        from src.core.marketdata_client import get_market_data

        us_rows = get_market_data().index_quotes(["usDJI", "usIXIC", "usINX"]) or []
        for r in us_rows:
            if not isinstance(r, dict):
                continue
            price = r.get("current_price")
            if price is None:
                continue
            items.append(
                {
                    "category": "us_index",
                    "symbol": str(r.get("symbol") or ""),
                    "name": str(r.get("name") or r.get("symbol") or ""),
                    "price": price,
                    "change_pct": r.get("change_pct"),
                    "source": "tencent",
                }
            )
        if us_rows:
            sources.append("tencent")
    except Exception as e:
        logger.debug("[证据层] 美股指数采集失败: %s", e)
        errors.append(f"美股指数:{e}")

    # 2. Yahoo 全球/亚太/期货指数
    try:
        from src.core.global_indices import fetch_global_indices

        g_map = fetch_global_indices() or {}
        if isinstance(g_map, dict) and g_map:
            existing_names = {it["name"] for it in items}
            for name, info in g_map.items():
                if not isinstance(info, dict) or name in existing_names:
                    continue
                items.append(
                    {
                        "category": "global_index",
                        "symbol": name,
                        "name": name,
                        "price": info.get("price"),
                        "change_pct": info.get("change_pct"),
                        "source": "yahoo",
                    }
                )
            sources.append("yahoo")
    except Exception as e:
        logger.debug("[证据层] 全球指数采集失败: %s", e)
        errors.append(f"全球指数:{e}")

    # 3. 大宗商品主力连续快照
    try:
        from src.core.commodity_quotes import fetch_snapshot

        snap = fetch_snapshot() or {}
        c_items = snap.get("items") or []
        if snap.get("available") and c_items:
            for c in c_items:
                if not isinstance(c, dict):
                    continue
                items.append(
                    {
                        "category": "commodity",
                        "symbol": str(c.get("code") or ""),
                        "name": str(c.get("name") or ""),
                        "price": c.get("last"),
                        "change_pct": c.get("chg_pct"),
                        "source": "sina_futures",
                    }
                )
            sources.append("sina_futures")
    except Exception as e:
        logger.debug("[证据层] 大宗商品采集失败: %s", e)
        errors.append(f"大宗商品:{e}")

    # 4. 汇率快照 (腾讯 qt.gtimg.cn whUSDCNY / whHKDCNY)
    try:
        from src.collectors.market_http import market_get

        raw_fx = market_get(
            "https://qt.gtimg.cn/q=whUSDCNY,whHKDCNY",
            host_key="qt.gtimg.cn",
            timeout=5,
            retries=1,
            parse="text",
            log_label="汇率快照",
        )
        if isinstance(raw_fx, str) and raw_fx.strip():
            fx_added = False
            for line in raw_fx.strip().split(";"):
                parts = line.strip().split("~")
                if len(parts) > 3:
                    try:
                        rate = float(parts[3])
                    except (TypeError, ValueError):
                        continue
                    if rate > 0:
                        items.append(
                            {
                                "category": "fx",
                                "symbol": str(parts[2] or ""),
                                "name": str(parts[1] or parts[2] or "汇率"),
                                "price": rate,
                                "change_pct": None,
                                "source": "tencent_fx",
                            }
                        )
                        fx_added = True
            if fx_added:
                sources.append("tencent_fx")
    except Exception as e:
        logger.debug("[证据层] 汇率采集失败: %s", e)
        errors.append(f"汇率:{e}")

    if not items:
        reason = "; ".join(errors) if errors else "隔夜外盘与大宗数据均为空"
        return _make_block([], "missing", reason=reason, source="none", as_of=as_of)

    return _make_block(
        items,
        "ok",
        reason="",
        source="+".join(sources) or "overnight",
        as_of=as_of,
    )


def _collect_ladder_block(as_of: str) -> EvidenceBlock:
    """采集涨停家数/连板梯队/板块分布，并做取数层失真审计。"""
    try:
        from src.collectors.market_sentiment_collector import MarketSentimentCollector

        summary = MarketSentimentCollector().get_sentiment_summary()
    except Exception as e:
        logger.warning("[证据层] 涨停梯队采集异常: %s", e)
        return _make_block(
            [],
            "missing",
            reason=f"涨停复盘数据获取失败: {e}",
            source="none",
            as_of=as_of,
        )

    quality, reason = audit_ladder_summary(summary)
    if quality == "missing":
        return _make_block(
            [],
            "missing",
            reason=reason or "数据获取失败",
            source="none",
            as_of=as_of,
        )

    source = str((summary or {}).get("limit_up_source") or "wudao/东财")
    return _make_block(
        [dict(summary)],
        quality,
        reason=reason,
        source=source,
        as_of=as_of,
    )


def _collect_news_block(as_of: str, limit: int = 15) -> EvidenceBlock:
    """采集隔夜/盘前快讯，执行时间戳失真审计并附带主题聚类与热度。"""
    try:
        from src.core.marketdata_client import get_market_data

        arts = get_market_data().flash_news(market="CN", limit=limit) or []
        raw_items: list[dict[str, Any]] = []
        for a in arts:
            if isinstance(a, dict):
                raw_items.append(dict(a))
                continue
            try:
                t = a.publish_time.strftime("%m-%d %H:%M")
            except Exception:
                t = ""
            raw_items.append(
                {
                    "time": t,
                    "title": getattr(a, "title", ""),
                    "source": getattr(a, "source", ""),
                    "importance": getattr(a, "importance", 0),
                    "url": getattr(a, "url", ""),
                    "symbols": list(getattr(a, "symbols", None) or []),
                }
            )
    except Exception as e:
        logger.warning("[证据层] 快讯获取失败: %s", e)
        return _make_block(
            [],
            "missing",
            reason=f"快讯获取失败: {e}",
            source="none",
            as_of=as_of,
        )

    quality, reason = audit_news_items(raw_items)
    if quality == "missing":
        return _make_block(
            [],
            "missing",
            reason=reason or "数据获取失败(或暂无快讯)",
            source="none",
            as_of=as_of,
        )

    enriched = enrich_news_clusters(raw_items)
    return _make_block(
        enriched,
        quality,
        reason=reason,
        source="cls/ths",
        as_of=as_of,
    )


def _collect_sentiment_block(as_of: str) -> EvidenceBlock:
    """采集情绪周期 (`mood_cycle.current_mood`)。"""
    try:
        from src.core.mood_cycle import current_mood

        mood = current_mood() or {}
    except Exception as e:
        logger.warning("[证据层] 情绪周期读取失败: %s", e)
        return _make_block(
            [],
            "missing",
            reason=f"情绪周期读取失败: {e}",
            source="none",
            as_of=as_of,
        )

    if not isinstance(mood, dict) or not mood.get("available"):
        reason = str((mood if isinstance(mood, dict) else {}).get("reason") or "无情绪周期数据")
        return _make_block(
            [],
            "missing",
            reason=reason,
            source="market_phase",
            as_of=as_of,
        )

    return _make_block(
        [dict(mood)],
        "ok",
        reason="",
        source="market_phase",
        as_of=as_of,
    )


def _collect_watchlist_block(
    db: Any, user_id: str | None, as_of: str, limit: int = 8
) -> EvidenceBlock:
    """采集今日关注候选 (`entry_candidates` 表最新 active 快照)。"""
    if db is None:
        return _make_block(
            [],
            "missing",
            reason="数据库会话未提供",
            source="entry_candidates",
            as_of=as_of,
        )
    try:
        from sqlalchemy import func as sa_func

        from src.db.models import EntryCandidate

        latest = db.query(sa_func.max(EntryCandidate.snapshot_date)).scalar()
        if not latest:
            return _make_block(
                [],
                "missing",
                reason="暂无候选(entry_candidates 表为空)",
                source="entry_candidates",
                as_of=as_of,
            )
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
        items = [
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
        if not items:
            return _make_block(
                [],
                "missing",
                reason="暂无候选(entry_candidates 无 active 记录)",
                source="entry_candidates",
                as_of=as_of,
            )
        return _make_block(
            items,
            "ok",
            reason="",
            source="entry_candidates",
            as_of=as_of,
        )
    except Exception as e:
        logger.warning("[证据层] 关注候选读取失败: %s", e)
        return _make_block(
            [],
            "missing",
            reason=f"关注候选读取失败: {e}",
            source="entry_candidates",
            as_of=as_of,
        )


def _collect_portfolio_block(
    db: Any, user_id: str | None, as_of: str, with_quotes: bool = True
) -> EvidenceBlock:
    """采集持仓及行情 (`positions` join `stocks` + `md_quote_rows`)。"""
    # 2026-10-29 四审: 共享口径(user_id=None)不采任何持仓 —— 共享桶里仍是个人的历史
    # 成本, 写进 scope=shared 的报告等于把它贴成「公共数据」。私有持仓只在按用户生成
    # 的报告里出现。
    if not user_id:
        return _make_block(
            [],
            "missing",
            reason="共享口径不含持仓(个人持仓见个人报告)",
            source="positions",
            as_of=as_of,
        )
    if db is None:
        return _make_block(
            [],
            "missing",
            reason="数据库会话未提供",
            source="positions",
            as_of=as_of,
        )
    try:
        from src.core.portfolio_scope import (
            account_user_scope_clause,
            position_user_scope_clause,
        )
        from src.db.models import Account, Position, Stock

        q = (
            db.query(Position, Stock)
            .join(Stock, Position.stock_id == Stock.id)
            .join(Account, Account.id == Position.account_id)
            .filter(
                Position.quantity > 0,
                # 2026-10-29 三审: 旧写法 `if user_id: filter(...)` 在 user_id=None 时
                # **一条过滤都不加** → 报告/证据层读到全库所有租户的持仓(成本/仓位)。
                # 现在统一走作用域条件: None = 历史共享桶(两侧 IS NULL), 绝不放开全表。
                account_user_scope_clause(user_id),
                position_user_scope_clause(user_id),
            )
        )
        rows = q.all()
    except Exception as e:
        logger.warning("[证据层] 持仓读取失败: %s", e)
        return _make_block(
            [],
            "missing",
            reason=f"持仓读取失败: {e}",
            source="positions",
            as_of=as_of,
        )

    if not rows:
        return _make_block(
            [],
            "missing",
            reason="暂无持仓数据",
            source="positions",
            as_of=as_of,
        )

    sym_by_market: dict[str, list[str]] = {}
    meta: dict[tuple[str, str], tuple[Any, Any]] = {}
    for pos, st in rows:
        sym_by_market.setdefault(st.market, []).append(st.symbol)
        meta[(st.market, st.symbol)] = (pos, st)

    quote_map: dict[tuple[str, str], dict[str, Any]] = {}
    quote_err_msg = ""
    if with_quotes:
        try:
            from src.core.marketdata_client import md_quote_rows

            for market, syms in sym_by_market.items():
                qs = md_quote_rows(syms, market) or []
                for q_row in qs:
                    if isinstance(q_row, dict):
                        quote_map[(q_row.get("market", market), q_row.get("symbol"))] = q_row
        except Exception as e:
            logger.warning("[证据层] 持仓行情获取失败: %s", e)
            quote_err_msg = str(e)

    items: list[dict[str, Any]] = []
    missing_quotes = 0
    for (market, sym), (pos, st) in meta.items():
        q_row = quote_map.get((market, sym))
        row: dict[str, Any] = {
            "symbol": sym,
            "name": st.name,
            "market": market,
            "cost_price": pos.cost_price,
            "quantity": pos.quantity,
            "trading_style": pos.trading_style or "swing",
        }
        if q_row:
            row["current_price"] = q_row.get("current_price")
            row["change_pct"] = q_row.get("change_pct")
            row["prev_close"] = q_row.get("prev_close")
            if pos.cost_price and q_row.get("current_price"):
                row["pnl_pct"] = (
                    (q_row["current_price"] - pos.cost_price) / pos.cost_price * 100
                )
        else:
            row["quote_error"] = "数据获取失败"
            missing_quotes += 1
        items.append(row)

    if with_quotes and missing_quotes == len(items):
        return _make_block(
            items,
            "suspect",
            reason=f"持仓行情获取失败{': ' + quote_err_msg if quote_err_msg else ''}",
            source="positions",
            as_of=as_of,
        )

    return _make_block(
        items,
        "ok",
        reason="",
        source="positions",
        as_of=as_of,
    )


# ──────────────────────────── 四档资金结构（第 7 块，2026-10-23） ────────────────────────────

#: 单轮最多取几只（TQ 单票 24~26ms，15 只 ≈ 0.4s，可接受；不要拖慢盘前 08:30 生成）
_FUND_STRUCTURE_MAX_SYMBOLS = 15


def _collect_fund_structure_block(
    db: Any, user_id: str | None, as_of: str
) -> EvidenceBlock:
    """采集自选 + 持仓的四档资金结构（客户端 `get_exday_data` 口径 tdx_exday4）。

    为什么单独一块：其余六块讲"发生了什么"，这一块讲**钱是哪一档在动**
    （散户化率、特大单占比、主力主动净额、拆单隐匿度），是盘前判断"这票有没有
    大资金在场"最直接的结构证据，而原来的六块完全没有这个维度。

    取数纪律：单票失败 → 跳过并计数，不伪造；全部失败 → `missing`；
    任一条 quality != ok → 整块标 `suspect`（数字仍透传，但带原因）。
    """
    # 共享口径不读任何人的自选/持仓(同 _collect_portfolio_block 的理由)。
    if not user_id:
        return _make_block(
            [],
            "missing",
            reason="共享口径不含自选/持仓, 四档结构无标的可算",
            source="exday_daily/get_exday_data",
            as_of=as_of,
        )
    if db is None:
        return _make_block([], "missing", reason="数据库会话未提供",
                           source="exday_daily/get_exday_data", as_of=as_of)
    # 自选 = `stocks.user_id`（与 copilot_schedule._watchlist_symbols 同口径，Stock 表
    # 没有 is_deleted 列，别照抄别处的过滤条件）；持仓 = positions join stocks。
    # 两段各自 try：自选表结构变了不该连带持仓也取不到。
    symbols: list[str] = []
    try:
        from src.core.portfolio_scope import stock_user_scope_clause
        from src.db.models import Stock

        # 2026-10-29 三审: 旧 `if user_id: filter(Stock.user_id==uid)` 在 user_id=None
        # 时不加过滤 → 全库自选(他人私有)进了证据块。共享桶口径 = stocks.user_id IS NULL。
        q = db.query(Stock.symbol).filter(stock_user_scope_clause(user_id))
        for (sym,) in q.limit(_FUND_STRUCTURE_MAX_SYMBOLS).all():
            if sym:
                symbols.append(str(sym).strip())
    except Exception as e:  # noqa: BLE001
        logger.debug("[证据层] 四档结构取自选失败: %s", e)
    try:
        from src.core.portfolio_scope import (
            account_user_scope_clause,
            position_user_scope_clause,
        )
        from src.db.models import Account, Position, Stock

        q = (
            db.query(Stock.symbol)
            .join(Position, Position.stock_id == Stock.id)
            .join(Account, Account.id == Position.account_id)
            .filter(
                Position.quantity > 0,
                account_user_scope_clause(user_id),
                position_user_scope_clause(user_id),
            )
        )
        for (sym,) in q.limit(_FUND_STRUCTURE_MAX_SYMBOLS).all():
            if sym:
                symbols.append(str(sym).strip())
    except Exception as e:  # noqa: BLE001
        logger.debug("[证据层] 四档结构取持仓失败: %s", e)

    symbols = list(dict.fromkeys(symbols))[:_FUND_STRUCTURE_MAX_SYMBOLS]
    if not symbols:
        return _make_block([], "missing", reason="无自选/持仓，四档结构无标的可算",
                           source="exday_daily/get_exday_data", as_of=as_of)

    items: list[dict[str, Any]] = []
    failed = 0
    suspect_reasons: list[str] = []
    try:
        from src.core import exday_structure as ex
    except Exception as e:  # noqa: BLE001
        return _make_block([], "missing", reason=f"exday_structure 不可用: {e}",
                           source="exday_daily/get_exday_data", as_of=as_of)

    for sym in symbols:
        try:
            m = ex.fetch(sym)
        except Exception:  # noqa: BLE001 - 单票失败只记数
            failed += 1
            continue
        if not m.get("available"):
            failed += 1
            continue
        f = m.get("factors") or {}
        g = m.get("granularity") or {}
        items.append({
            "symbol": str(sym),
            "amount": m.get("amount"),
            "active_ratio": m.get("active_ratio"),
            "retail_ratio": f.get("retail_ratio"),
            "xl_share": f.get("xl_share"),
            "main_active_net": f.get("main_active_net"),
            "chip_migration": f.get("chip_migration"),
            "gran_hiding": g.get("gran_hiding"),
            "order_fill_ratio": g.get("order_fill_ratio"),
            "cancel_skew": g.get("cancel_skew"),
            "quality": m.get("quality"),
            "tags": [t.get("label") for t in (m.get("tags") or [])],
        })
        if m.get("quality") != "ok" and m.get("reason"):
            suspect_reasons.append(f"{sym}: {m.get('reason')}")

    if not items:
        return _make_block([], "missing",
                           reason=f"{len(symbols)} 只标的全部取数失败（TQ 未开/非交易日）",
                           source="exday_daily/get_exday_data", as_of=as_of)

    if suspect_reasons:
        return _make_block(items, "suspect",
                           reason="；".join(suspect_reasons)[:300],
                           source="exday_daily/get_exday_data", as_of=as_of)
    reason = f"{failed} 只取数失败（其余 {len(items)} 只正常）" if failed else ""
    return _make_block(items, "ok", reason=reason,
                       source="exday_daily/get_exday_data", as_of=as_of)


# ──────────────────────────── 统一入口 ────────────────────────────


def collect(
    date: str | None = None,
    db: Any = None,
    user_id: str | None = None,
) -> PremarketEvidence:
    """采集盘前全部结构化证据。

    永不抛异常：任何子块失败均在块内标记 `quality="missing"` 并附带 `reason`。

    `user_id` 作用域(2026-10-29 三审, fail-closed):
      传 uid → 只采该用户可见的持仓/自选(账户可见 AND 持仓可见);
      传 None(**显式共享桶**) → 只采 `Account.user_id IS NULL AND Position.user_id IS NULL`
      与 `Stock.user_id IS NULL` 的历史共享项, **绝不**等于"全库所有用户"。
    任何缺会话路径因此自然收敛, 报告不会再把别人的成本/仓位写进共享文件。
    """
    date_iso, _ = _normalize_date(date)
    as_of = _now_iso()

    own_db = False
    if db is None:
        try:
            from src.db.session import SessionLocal

            db = SessionLocal()
            own_db = True
        except Exception as e:
            logger.warning("[证据层] 数据库连接初始化失败: %s", e)
            db = None

    try:
        overnight = _collect_overnight_block(as_of)
        ladder = _collect_ladder_block(as_of)
        news = _collect_news_block(as_of)
        sentiment = _collect_sentiment_block(as_of)
        watchlist = _collect_watchlist_block(db, user_id, as_of)
        portfolio = _collect_portfolio_block(db, user_id, as_of)
        fund_structure = _collect_fund_structure_block(db, user_id, as_of)
    finally:
        if own_db and db is not None:
            try:
                db.close()
            except Exception:
                pass

    return {
        "date": date_iso,
        "as_of": as_of,
        "user_id": user_id,
        "overnight": overnight,
        "ladder": ladder,
        "news": news,
        "sentiment": sentiment,
        "watchlist": watchlist,
        "portfolio": portfolio,
        "fund_structure": fund_structure,
    }
