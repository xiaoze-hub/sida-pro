"""PanWatch ↔ marketdata 接线:DB 配置端口 + 单例 + flag 门控的报价兼容层。

- DbConfigProvider:把 DataSource 表映射成 marketdata 的 SourceConfig(实现 ConfigProvider 端口)。
- get_market_data():进程级单例(无状态 vendor + 现查 DB 的配置端口)。
- md_quote_rows():新包 MarketData.quotes 转 dict,返回 list[dict](与旧 orchestrator 输出同形)。
- md_news()/md_news_by_keyword():新包 MarketData.news/news_by_keyword 转 host NewsItem。
"""

from __future__ import annotations

import logging

from marketdata import MarketData, Quote, SourceConfig

logger = logging.getLogger(__name__)


class DbConfigProvider:
    """ConfigProvider 端口实现:从 DataSource 表按 priority 读某类型的启用源。"""

    def _query_rows(self, datatype: str) -> list:
        from src.db.session import SessionLocal
        from src.db.models import DataSource

        db = SessionLocal()
        try:
            return (
                db.query(DataSource)
                .filter(DataSource.type == datatype, DataSource.enabled == True)  # noqa: E712
                .order_by(DataSource.priority)
                .all()
            )
        finally:
            db.close()

    def sources_for(self, datatype: str, market: str | None) -> list[SourceConfig]:
        rows = self._query_rows(datatype)
        out: list[SourceConfig] = []
        for r in rows:
            cfg = r.config or {}
            # 多 key 池: 从 config["api_keys"] 提取(同源多个凭证, 由 Engine 的 KeyPool 轮换)
            key_pool = cfg.get("api_keys") or []
            if isinstance(key_pool, str):
                key_pool = [key_pool]
            out.append(SourceConfig(
                vendor=r.provider,
                priority=r.priority,
                enabled=True,
                config=cfg,
                supports_batch=bool(r.supports_batch),
                key_pool=list(key_pool),
            ))
        return out


_md: MarketData | None = None


def get_market_data() -> MarketData:
    """进程级单例。vendor 无状态、配置现查 DB,故无需失效钩子。

    批次1-B(2026-09-10): 注入 **Redis 共享缓存**(跨 uvicorn worker), 消除
    "同标的被两个 worker 各拉一次"; 未配置 Redis / Redis 故障时自动退回进程内缓存
    (行为与改造前一致, 不会退化成"无缓存风暴")。
    """
    global _md
    if _md is None:
        from src.core.md_redis_cache import redis_cache_factory

        _md = MarketData(config=DbConfigProvider(), cache_factory=redis_cache_factory())
    return _md


def reset_market_data() -> None:
    """测试或热重载时重置单例。"""
    global _md
    _md = None


def _quote_to_row(q: Quote) -> dict:
    """marketdata.Quote → 旧 orchestrator 同形 dict。"""
    return {
        "symbol": q.symbol,
        "name": q.name,
        "market": q.market,
        "current_price": q.current_price,
        "change_pct": q.change_pct,
        "change_amount": q.change_amount,
        "prev_close": q.prev_close,
        "open_price": q.open_price,
        "high_price": q.high_price,
        "low_price": q.low_price,
        "volume": q.volume,
        "turnover": q.turnover,
        "turnover_rate": q.turnover_rate,
        "volume_ratio": q.volume_ratio,
        "pe_ratio": q.pe_ratio,
        "pb_ratio": q.pb_ratio,
        "circulating_market_value": q.circulating_market_value,
        "total_market_value": q.total_market_value,
        "quote_time": q.quote_time.isoformat() if q.quote_time else None,
        "quote_date": q.quote_time.date().isoformat() if q.quote_time else None,
        # 2026-09-08 来源透传: 实际命中的 vendor(空=未知, 前端按"未知源"标, 不编造)
        "source": getattr(q, "source", "") or "",
        "source_latency_ms": getattr(q, "latency_ms", 0) or 0,
        # 2026-09-08 (风险方案1.1) 完整性透传: ok/partial/missing + 缺失字段清单。
        # 前端对 partial/missing 与 null 字段显式标「无数据」, 不允许拿 0 画图。
        "status": getattr(q, "status", "ok") or "ok",
        "missing_fields": list(getattr(q, "missing_fields", []) or []),
    }


def md_quote_rows(symbols: list[str], market: str) -> list[dict]:
    """批量报价,返回 list[dict](与旧 orchestrator 输出同形)。

    同步函数;async 调用方用 `await asyncio.to_thread(md_quote_rows, ...)`。
    """
    syms = list(symbols)
    if not syms:
        return []
    quotes = get_market_data().quotes(syms, market=market)
    return [_quote_to_row(q) for q in quotes]


def _article_to_newsitem(a):
    """marketdata.NewsArticle → host NewsItem(同名字段直拷)。

    lazy import 避免与 news_collector 的模块级循环引用(news_collector 会
    在模块级 import 本模块的 md_news)。
    """
    from src.collectors.news_collector import NewsItem

    return NewsItem(
        source=a.source,
        external_id=a.external_id,
        title=a.title,
        content=a.content,
        publish_time=a.publish_time,
        symbols=a.symbols,
        importance=a.importance,
        url=a.url,
    )


def md_news(
    symbols: list[str], since_hours: int = 2, names: dict[str, str] | None = None
) -> list:
    """聚合新闻(个股新闻 + 公告),返回 list[NewsItem](与旧 NewsCollector.fetch_all 同形)。

    host 侧可以用 datetime.now() 做 since 过滤(包内不允许偷偷调 datetime.now(),
    必须由调用方显式传 now)。

    同步函数;async 调用方用 `await asyncio.to_thread(md_news, ...)`。
    """
    from datetime import datetime, timezone

    # 包内 news vendor 的 publish_time 是 aware(UTC);这里的 now 也必须 aware,
    # 否则 since 过滤会 "can't compare offset-naive and offset-aware datetimes"。
    arts = get_market_data().news(
        list(symbols or []), since_hours=since_hours, names=names,
        now=datetime.now(timezone.utc),
    )
    return [_article_to_newsitem(a) for a in arts]


def md_news_by_keyword(keyword: str) -> list:
    """按关键词(行业/主题词)搜中文新闻,返回 list[NewsItem]。同步。"""
    arts = get_market_data().news_by_keyword(keyword)
    return [_article_to_newsitem(a) for a in arts]


def md_stock_data(symbols: list[str], market: str) -> list:
    """返回 list[StockData](旧 AkshareCollector.get_stock_data 同形)。同步。

    2026-09-08 (风险方案1.1): 缺价 Quote(current_price is None)在此跳过 = 调用方按
    「无数据」处理, 绝不回退 0.0 参与 agent 算术。partial Quote 照常进入, status
    随行, 但注意 StockData 数值字段是旧契约(非可选), 缺失数值仍会落 0.0 ——
    消费方应先看 status 再信任数值。
    """
    from src.models.market import MarketCode, StockData

    syms = list(symbols)
    if not syms:
        return []
    quotes = get_market_data().quotes(syms, market=market)
    return [StockData(
        symbol=q.symbol, name=q.name or "", market=MarketCode(q.market),
        current_price=q.current_price or 0.0, change_pct=q.change_pct or 0.0,
        change_amount=q.change_amount or 0.0, volume=q.volume or 0.0,
        turnover=q.turnover or 0.0, open_price=q.open_price or 0.0,
        high_price=q.high_price or 0.0, low_price=q.low_price or 0.0,
        prev_close=q.prev_close or 0.0,
        volume_ratio=getattr(q, "volume_ratio", None),
        status=getattr(q, "status", "ok") or "ok",
    ) for q in quotes if q.current_price is not None]


def md_more_info(symbols: list[str], market: str = "CN") -> list[dict]:
    """TQ 扩展指标(104字段), 返回 list[dict] 供 API 透传。同步。"""
    items = get_market_data().more_info(symbols, market=market)
    out = []
    for m in items:
        out.append({
            "symbol": m.symbol,
            "market": m.market,
            "turnover_rate": m.turnover_rate,
            "volume_ratio": m.volume_ratio,
            "commission_ratio": m.commission_ratio,
            "total_market_value": m.total_market_value,
            "circulating_market_value": m.circulating_market_value,
            "change_pct": m.change_pct,
            "change_pct_5d": m.change_pct_5d,
            "change_pct_20d": m.change_pct_20d,
            "change_pct_ytd": m.change_pct_ytd,
            "limit_up_amount": m.limit_up_amount,
            "limit_up_ratio": m.limit_up_ratio,
            "open_amount": m.open_amount,
            "open_limit_buy": m.open_limit_buy,
            "consecutive_limit_days": m.consecutive_limit_days,
            "consecutive_up_days": m.consecutive_up_days,
            "pe_dynamic": m.pe_dynamic,
            "pe_ttm": m.pe_ttm,
            "pb": m.pb,
            "dividend_yield": m.dividend_yield,
            "beta": m.beta,
            "ma5_price": m.ma5_price,
            "high_52w": m.high_52w,
            "low_52w": m.low_52w,
            "l2_tick_num": m.l2_tick_num,
            "l2_order_num": m.l2_order_num,
            "total_buy_vol": m.total_buy_vol,
            "total_sell_vol": m.total_sell_vol,
            "cancel_buy": m.cancel_buy,
            "cancel_sell": m.cancel_sell,
            # 2026-09-09: 资金两字段此前漏映射 → 前端「主力净额/主买净额」恒显 "--"
            "zjl": m.zjl,
            "zjl_hb": m.zjl_hb,
            "raw": m.raw,
            "quote_time": m.quote_time.isoformat() if m.quote_time else None,
        })
    return out


def md_dark_flow_tq(symbol: str, market: str = "CN") -> dict | None:
    """读取 ZCode TQ4 采集的通达信 L2 暗盘资金 JSON(盘后, DATA_DIR/darkflow/)。

    文件命名: {symbol}.json 或 {symbol}_{yyyymmdd}.json, 同名多份取日期最新。
    无文件/读取失败 → 返回 None(API 层回 404, 前端显式标"盘后补全")。
    schema 契约见 packages/marketdata types.DarkFlowTq。
    """
    import glob
    import json as _json
    import os

    data_dir = os.environ.get("DATA_DIR", "/app/data")
    dark_dir = os.path.join(data_dir, "darkflow")
    candidates: list[str] = []
    exact = os.path.join(dark_dir, f"{symbol}.json")
    if os.path.isfile(exact):
        candidates.append(exact)
    candidates += sorted(glob.glob(os.path.join(dark_dir, f"{symbol}_*.json")), reverse=True)
    if not candidates:
        return None
    path = candidates[0]
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning("darkflow 读取失败 %s: %s", path, exc)
        return None


def md_formula_mul(formula_name: str, stock_list: list[str], **kw) -> dict:
    """批量通达信指标公式(TQ formula_process_mul_zb)。

    返回 {代码: {指标名: [值...]}}, 失败返回 {}。
    kw: stock_period/count/return_count/dividend_type/xsflag。
    内置公式: MACD / ZLJC(主力进出 JCL/JCM/JCS)。L2_AMO 需客户端自定义公式后按名调。
    """
    from marketdata.vendors.tq import formula_mul

    try:
        return formula_mul(formula_name, stock_list, **kw)
    except Exception as e:  # noqa: BLE001
        logger.warning("md_formula_mul %s failed: %s", formula_name, e)
        return {}


def md_main_flow_zljc(stock_list: list[str]) -> dict:
    """通达信 ZLJC(主力进出) 三档净量 → {代码: {jcl, jcm, jcs}}。

    JCL/JCM/JCS = 超/大/中单净量(通达信内置"主力进出"指标)。明盘主力资金流现成口径,
    与 get_more_info.Zjl_HB(主力净额) 互补: ZLJC 分三档、Zjl_HB 是含B净额。
    """
    raw = md_formula_mul("ZLJC", stock_list, return_count=1)
    out: dict = {}
    for code, metrics in raw.items():
        if not isinstance(metrics, dict):
            continue
        out[code] = {
            "jcl": (metrics.get("JCL") or [None])[0],
            "jcm": (metrics.get("JCM") or [None])[0],
            "jcs": (metrics.get("JCS") or [None])[0],
        }
    return out
