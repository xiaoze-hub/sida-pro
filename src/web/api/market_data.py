"""市场数据代理 API: 把 marketdata 包的关键方法暴露成只读 HTTP 端点。

设计原则(与 calendar.py 一致):
- 直接调用 marketdata 包的 vendor/Engine, **不重写数据源逻辑**
- config=None → vendor 自动从容器 DB 的 data_sources 表读 UI 维护的 key
  (即「设置 → 接口Key」配置的凭证, 改了立即生效, 无需重启)
- 供 8010 预测引擎在宿主机调用(宿主机无 marketdata 包, 经 8000 HTTP 取数)

暴露:
- GET /api/market-data/dragon-tiger/{date}  龙虎榜(ftshare vendor)
- GET /api/market-data/capital-flow/{symbol}  资金流(经 MarketData Engine, 走 UI 配置 vendor)
- GET /api/market-data/fundamentals-detail/{symbol}  个股基本面明细合并端点(龙虎榜/两融/股东户数/分红/事件日历)
- GET /api/market-data/anomalies  东财异动池(交易所「严重异常波动」口径, 供首页 Dashboard)
- GET /api/market-data/hot-stocks  同花顺热榜(小时榜/日榜, 含 AI 归因, 供首页 Dashboard)
- GET /api/market-data/market-capital-flow  大盘资金(对齐同花顺APP口径, 顺手写 30s 快照入 DB)
- GET /api/market-data/market-capital-flow/history?hours=4  当日大盘资金快照序列
- GET /api/market-data/breadth-distribution  全市场涨跌幅 9 档分桶(60s biz_cache)
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from src.web.cache.biz_cache import biz_cache

logger = logging.getLogger(__name__)

router = APIRouter()


# ──────────── Task 1: 大盘资金快照表(双方言兼容, v0.4.7) ────────────
# market-capital-flow 接口成功返回后, 异步写一条快照入 market_flow_snapshots;
# 同一进程 30s 节流(前端高频轮询不会撑爆表), 失败静默不阻断主流程。
_SNAPSHOT_INTERVAL_S = 30.0
_snapshot_lock = threading.Lock()
_snapshot_last_write_ts: float = 0.0
_snapshot_table_ready = False


def _ensure_snapshot_table() -> None:
    """幂等建表: 模块加载时跑一次, CREATE TABLE IF NOT EXISTS。

    双方言支持(SQLite / PostgreSQL):
      - SQLite: ts DATETIME DEFAULT CURRENT_TIMESTAMP, 主键 INTEGER
      - PG    : ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 主键 SERIAL
    字段语义(与接口返回口径一致):
      total_main_flow  两市主力净流入(亿元, 可负)
      up/down/flat_count  涨跌平家数(同花顺APP盘面口径)
      sh_flow / sz_flow  沪/深市主力净流入(亿元)
    """
    global _snapshot_table_ready
    if _snapshot_table_ready:
        return
    try:
        from src.web.database import IS_PG, engine
        if IS_PG:
            ddl = (
                """
                CREATE TABLE IF NOT EXISTS market_flow_snapshots (
                    id SERIAL PRIMARY KEY,
                    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_main_flow DOUBLE PRECISION,
                    up_count INTEGER,
                    down_count INTEGER,
                    flat_count INTEGER,
                    sh_flow DOUBLE PRECISION,
                    sz_flow DOUBLE PRECISION
                )
                """
            )
        else:
            ddl = (
                """
                CREATE TABLE IF NOT EXISTS market_flow_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                    total_main_flow REAL,
                    up_count INTEGER,
                    down_count INTEGER,
                    flat_count INTEGER,
                    sh_flow REAL,
                    sz_flow REAL
                )
                """
            )
        with engine.begin() as conn:
            conn.execute(text(ddl))
        _snapshot_table_ready = True
    except Exception as e:
        # 静默失败(避免模块加载拖垮整个进程); 真正写入时再尝试
        logger.debug(f"market_flow_snapshots 建表暂未就绪: {e}")


def _try_write_snapshot_async(payload: dict) -> None:
    """后台线程写一条大盘资金快照(失败静默, try/except + logger.debug)。

    payload 来自 market-capital-flow 接口聚合后的 dict, 仅取需要的字段。
    30s 节流: 同一进程内连续调用时只写第一条, 避免前端高频轮询拖垮表。
    """
    def _runner() -> None:
        try:
            from src.web.database import engine
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO market_flow_snapshots
                            (total_main_flow, up_count, down_count, flat_count,
                             sh_flow, sz_flow)
                        VALUES
                            (:total_main_flow, :up_count, :down_count, :flat_count,
                             :sh_flow, :sz_flow)
                        """
                    ),
                    {
                        "total_main_flow": payload.get("total_main_flow"),
                        "up_count": payload.get("up_count"),
                        "down_count": payload.get("down_count"),
                        "flat_count": payload.get("flat_count"),
                        "sh_flow": payload.get("sh_flow"),
                        "sz_flow": payload.get("sz_flow"),
                    },
                )
        except Exception as e:
            logger.debug(f"market_flow_snapshots 写入失败(静默): {e}")

    now = time.monotonic()
    with _snapshot_lock:
        global _snapshot_last_write_ts
        if now - _snapshot_last_write_ts < _SNAPSHOT_INTERVAL_S:
            return  # 节流窗口内, 跳过
        _snapshot_last_write_ts = now

    # 兜底: 首次写入时若建表未就绪, 再补一次
    if not _snapshot_table_ready:
        _ensure_snapshot_table()
    t = threading.Thread(target=_runner, name="mkt-flow-snapshot-writer", daemon=True)
    t.start()


# 模块加载时尝试一次建表(进程冷启动时不依赖首次请求)
_ensure_snapshot_table()


@router.get("/dragon-tiger/{trade_date}")
async def dragon_tiger_proxy(
    trade_date: str,
    market: str = Query("CN", description="市场"),
):
    """龙虎榜(经 marketdata dragon_tiger vendor, 主源东财 + ftshare 补席位)。

    trade_date: YYYYMMDD
    key 来自「设置→接口Key」配置的 data_sources(type=dragon_tiger), 实时生效。

    2026-08-20: 东财 datacenter 有汇总(净买/原因/上榜明细)无席位, ftshare 有席位
    但需全市场翻页。合并:东财做主, ftshare 补同 symbol 行的席位字段。
    """
    try:
        from src.core.marketdata_client import get_market_data
        md = get_market_data()
        rows = md.dragon_tiger(date=trade_date, market=market) or []

        # 补席位: 直接调 ftshare vendor(Engine 跳过 enabled=0, 走旁路)
        try:
            from marketdata.vendors.ftshare import FtshareDragonTigerVendor
            _ft = FtshareDragonTigerVendor()
            ft_rows = _ft.fetch([], {"date": trade_date, "market": market}) or []
            ft_by_sym = {r.symbol: r for r in ft_rows if r.symbol}
        except Exception:
            ft_by_sym = {}

        items = []
        for i in rows:
            ft = ft_by_sym.get(i.symbol)
            items.append(
                {
                    "trade_date": getattr(i, "trade_date", trade_date),
                    "symbol": getattr(i, "symbol", ""),
                    "name": getattr(i, "name", ""),
                    "close": getattr(i, "close", None),
                    "change_pct": getattr(i, "change_pct", None),
                    "net_buy": getattr(i, "net_buy", None),
                    "buy_amt": getattr(i, "buy_amt", None),
                    "sell_amt": getattr(i, "sell_amt", None),
                    "reason": getattr(i, "reason", None),
                    "top_buyers": list(getattr(ft, "top_buyers", []) or []) if ft else [],
                    "top_sellers": list(getattr(ft, "top_sellers", []) or []) if ft else [],
                }
            )
        return {
            "trade_date": trade_date,
            "market": market,
            "count": len(items),
            "items": items,
        }
    except Exception as e:
        logger.warning(f"龙虎榜代理失败 [{trade_date}]: {e}")
        raise HTTPException(502, f"数据源调用失败: {e}")


@router.get("/capital-flow/{symbol}")
async def capital_flow_proxy(
    symbol: str,
    market: str = Query("CN", description="市场"),
):
    """资金流(经 MarketData Engine, 走 UI 配置的 capital_flow vendor, 默认 sina/eastmoney)。

    key 来自「设置→接口Key」配置的 data_sources(type=capital_flow), 实时生效。
    """
    try:
        from src.core.marketdata_client import get_market_data
        md = get_market_data()
        cf = md.capital_flow(symbol, market=market)
        if cf is None:
            return {"symbol": symbol, "market": market, "error": "no_data"}
        return {
            "symbol": symbol,
            "market": market,
            "main_net_inflow": cf.main_net_inflow,
            "main_net_inflow_pct": cf.main_net_inflow_pct,
            "super_net_inflow": cf.super_net_inflow,
            "big_net_inflow": cf.big_net_inflow,
            "mid_net_inflow": cf.mid_net_inflow,
            "small_net_inflow": cf.small_net_inflow,
            "main_net_5d": cf.main_net_5d,
        }
    except Exception as e:
        logger.warning(f"资金流代理失败 [{symbol}]: {e}")
        raise HTTPException(502, f"数据源调用失败: {e}")


@router.get("/board-capital-flow")
async def board_capital_flow_proxy(
    board_type: str = Query("industry", description="industry 行业 / concept 概念"),
):
    """板块资金流向(同花顺行业/概念资金,免登录免费源)。

    返回按净额降序的板块资金列表(流入/流出/净额,单位亿)。
    """
    try:
        from src.core.marketdata_client import get_market_data
        md = get_market_data()
        boards = md.board_capital_flow(board_type=board_type)
        return {
            "board_type": board_type,
            "count": len(boards),
            "items": [
                {
                    "board_name": b.board_name,
                    "board_type": b.board_type,
                    "index_value": b.index_value,
                    "change_pct": b.change_pct,
                    "inflow": b.inflow,
                    "outflow": b.outflow,
                    "net_inflow": b.net_inflow,
                    "stock_count": b.stock_count,
                    "leader_name": b.leader_name,
                    "leader_change_pct": b.leader_change_pct,
                    "leader_price": b.leader_price,
                    "rank": b.rank,
                }
                for b in boards
            ],
        }
    except Exception as e:
        logger.warning(f"板块资金代理失败: {e}")
        raise HTTPException(502, f"数据源调用失败: {e}")


@router.get("/market-capital-flow")
async def market_capital_flow_proxy():
    """大盘资金(对齐同花顺APP口径: 两市主力净流入 + 总成交额 + 涨跌家数 + 板块明细)。

    2026-08-10 重构: 之前用同花顺 hyzjl 行业资金求和(总流入2611亿口径不对),
    改为国内网关东财两市主力净流入(超大单+大单汇总, 与APP一致)。
    """
    try:
        import requests as _req
        # 1. 国内网关: 两市主力净流入 + 成交额 + 涨跌家数
        ov = _req.get(
            "http://115.190.177.213:8100/cn/market-overview", timeout=6
        ).json()
        if ov.get("error"):
            return {"error": ov["error"]}
        # 2. 板块资金明细(同花顺 hyzjl 行业, 流入/流出榜)
        from src.core.marketdata_client import get_market_data
        md = get_market_data()
        boards = md.board_capital_flow(board_type="industry") or []
        boards_sorted = sorted(
            boards, key=lambda b: (b.net_inflow or 0.0), reverse=True
        )
        inflow_boards = [
            {
                "name": b.board_name,
                "net_inflow": round(b.net_inflow or 0.0, 2),  # 亿
                "change_pct": b.change_pct,
            }
            for b in boards_sorted[:10]
            if (b.net_inflow or 0.0) > 0
        ]
        outflow_boards = [
            {
                "name": b.board_name,
                "net_inflow": round(b.net_inflow or 0.0, 2),  # 亿(负=流出)
                "change_pct": b.change_pct,
            }
            for b in reversed(boards_sorted[-10:])
            if (b.net_inflow or 0.0) < 0
        ]
        result = {
            # 两市主力净流入(对齐同花顺APP)
            "total_main_flow": ov.get("total_main_flow"),      # 亿
            "sh_flow": (ov.get("sh") or {}).get("main_flow"),  # 沪市主力
            "sz_flow": (ov.get("sz") or {}).get("main_flow"),  # 深市主力
            "cyb_flow": (ov.get("cyb") or {}).get("main_flow"),
            # 市场统计(同花顺APP盘面)
            "total_amount": ov.get("total_amount"),            # 两市成交额亿
            "up_count": ov.get("up_count"),
            "down_count": ov.get("down_count"),
            "flat_count": ov.get("flat_count"),
            "sh": ov.get("sh"), "sz": ov.get("sz"), "cyb": ov.get("cyb"),
            # 板块明细
            "inflow_boards": inflow_boards,
            "outflow_boards": outflow_boards,
            "source": "eastmoney_push2delay_cn",
            "timestamp": None,
        }
        # v0.4.7: 顺手异步写库(30s 节流, 失败静默不阻断接口)
        try:
            _try_write_snapshot_async(result)
        except Exception:
            pass
        return result
    except Exception as e:
        logger.warning(f"大盘资金代理失败: {e}")
        raise HTTPException(502, f"数据源调用失败: {e}")


# ──────────── 大盘资金快照历史(v0.4.7) ────────────
@router.get("/market-capital-flow/history")
async def market_capital_flow_history(
    hours: int = Query(4, ge=1, le=24, description="回溯小时数(默认 4h, 上限 24h)"),
):
    """读取 market_flow_snapshots 序列(按 ts 升序), 上限 500 条。

    返回 [{ts, total_main_flow, up_count, down_count, sh_flow, sz_flow}, ...]
    数据缺失/库不可达: 返回空数组 + note(前端展示"暂无快照"占位)。
    """
    try:
        from src.web.database import engine
        # 用 datetime/timedelta 计算 cutoff; SQLite 与 PG 都接受 ISO 字符串
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(hours=max(1, min(int(hours), 24)))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT ts, total_main_flow, up_count, down_count,
                           sh_flow, sz_flow
                    FROM market_flow_snapshots
                    WHERE ts >= :cutoff
                    ORDER BY ts ASC
                    LIMIT 500
                    """
                ),
                {"cutoff": cutoff},
            ).fetchall()
        items = []
        for r in rows:
            ts_val = r[0]
            # 统一把 datetime/timestamp 转 ISO 字符串, 给前端 timeline 用
            try:
                if hasattr(ts_val, "isoformat"):
                    ts_str = ts_val.isoformat()
                else:
                    ts_str = str(ts_val)
            except Exception:
                ts_str = str(ts_val)
            items.append(
                {
                    "ts": ts_str,
                    "total_main_flow": float(r[1]) if r[1] is not None else None,
                    "up_count": int(r[2]) if r[2] is not None else None,
                    "down_count": int(r[3]) if r[3] is not None else None,
                    "sh_flow": float(r[4]) if r[4] is not None else None,
                    "sz_flow": float(r[5]) if r[5] is not None else None,
                }
            )
        return {
            "hours": hours,
            "count": len(items),
            "items": items,
            "note": "" if items else "暂无快照(等待大盘资金接口写入)",
        }
    except Exception as e:
        logger.warning(f"大盘资金历史读取失败: {e}")
        return {
            "hours": hours,
            "count": 0,
            "items": [],
            "note": f"读取失败: {e}",
        }


# ──────────────── 个股基本面明细合并(龙虎榜/两融/股东户数/分红/事件日历) ────────────────


def fetch_fundamentals_detail(symbol: str, market: str = "CN", dt_days: int = 10) -> dict:
    """个股基本面明细合并取数(纯函数, 供 HTTP 端点与对话助手共用)。

    - dragon_tiger: 市场级按日接口, 回溯最近 dt_days 个自然日并按 symbol 过滤
    - margin / shareholders / dividend / events: 按 symbol 批量接口
    每类独立容错: 单类 vendor 失败只记日志、该类别返回空数组, 不拖垮整体。
    """
    from datetime import date, timedelta

    from src.core.marketdata_client import get_market_data

    md = get_market_data()
    out: dict = {
        "symbol": symbol,
        "market": market,
        "dragon_tiger": [],
        "margin": [],
        "shareholders": [],
        "dividend": [],
        "events": [],
    }

    # 1) 龙虎榜(市场级按日, 回溯 dt_days 天按 symbol 过滤; 引擎内存缓存, 重复日期不重复抓)
    # 2026-08-20: 同时拉 ftshare 补席位明细(东财 datacenter 不公开席位)
    try:
        from marketdata.vendors.ftshare import FtshareDragonTigerVendor
        _ft = FtshareDragonTigerVendor()
    except Exception:
        _ft = None
    scanned = max(1, min(int(dt_days), 30))
    d = date.today()
    for _ in range(scanned):
        ds = d.strftime("%Y%m%d")
        d -= timedelta(days=1)
        try:
            rows = md.dragon_tiger(date=ds, market=market) or []
        except Exception as e:
            logger.warning(f"基本面明细-龙虎榜[{ds}]查询失败(跳过): {e}")
            continue
        # ftshare 旁路补席位(只在该日期有上榜记录时调, 避免空查)
        ft_by_sym: dict = {}
        if _ft and rows:
            try:
                ft_rows = _ft.fetch([], {"date": ds, "market": market}) or []
                ft_by_sym = {r.symbol: r for r in ft_rows if r.symbol}
            except Exception:
                pass
        for i in rows:
            if getattr(i, "symbol", "") != symbol:
                continue
            ft = ft_by_sym.get(symbol)
            out["dragon_tiger"].append(
                {
                    "trade_date": getattr(i, "trade_date", ds),
                    "symbol": getattr(i, "symbol", symbol),
                    "name": getattr(i, "name", ""),
                    "reason": getattr(i, "reason", None),
                    "close": getattr(i, "close", None),
                    "change_pct": getattr(i, "change_pct", None),
                    "net_buy": getattr(i, "net_buy", None),
                    "buy_amt": getattr(i, "buy_amt", None),
                    "sell_amt": getattr(i, "sell_amt", None),
                    "turnover_pct": getattr(i, "turnover_pct", None),
                    "top_buyers": list(getattr(ft, "top_buyers", []) or []) if ft else [],
                    "top_sellers": list(getattr(ft, "top_sellers", []) or []) if ft else [],
                }
            )
    # 龙虎榜按交易日倒序(新→旧)
    out["dragon_tiger"].sort(key=lambda r: r["trade_date"] or "", reverse=True)

    # 2) 融资融券(按 symbol, 取最新快照)
    try:
        for i in md.margin([symbol], market=market) or []:
            out["margin"].append(
                {
                    "date": getattr(i, "date", ""),
                    "symbol": getattr(i, "symbol", symbol),
                    "rz_balance": getattr(i, "rz_balance", None),
                    "rz_buy": getattr(i, "rz_buy", None),
                    "rz_repay": getattr(i, "rz_repay", None),
                    "rq_balance": getattr(i, "rq_balance", None),
                    "rq_sell_vol": getattr(i, "rq_sell_vol", None),
                    "rq_repay_vol": getattr(i, "rq_repay_vol", None),
                    "total_balance": getattr(i, "total_balance", None),
                }
            )
    except Exception as e:
        logger.warning(f"基本面明细-两融[{symbol}]查询失败: {e}")

    # 3) 股东户数(按 symbol, 取最新一期)
    try:
        for i in md.shareholders([symbol], market=market) or []:
            out["shareholders"].append(
                {
                    "report_date": getattr(i, "report_date", ""),
                    "symbol": getattr(i, "symbol", symbol),
                    "holder_num": getattr(i, "holder_num", None),
                    "change_num": getattr(i, "change_num", None),
                    "change_ratio": getattr(i, "change_ratio", None),
                    "avg_shares": getattr(i, "avg_shares", None),
                }
            )
    except Exception as e:
        logger.warning(f"基本面明细-股东户数[{symbol}]查询失败: {e}")

    # 4) 分红(按 symbol, 全部历史, 按除权日倒序)
    try:
        for i in md.dividend([symbol], market=market) or []:
            out["dividend"].append(
                {
                    "ex_date": getattr(i, "ex_date", ""),
                    "symbol": getattr(i, "symbol", symbol),
                    "dividend_per_share": getattr(i, "dividend_per_share", None),
                    "transfer_ratio": getattr(i, "transfer_ratio", None),
                    "bonus_ratio": getattr(i, "bonus_ratio", None),
                    "progress": getattr(i, "progress", ""),
                }
            )
        out["dividend"].sort(key=lambda r: r["ex_date"] or "", reverse=True)
    except Exception as e:
        logger.warning(f"基本面明细-分红[{symbol}]查询失败: {e}")

    # 5) 事件日历(按 symbol, 近 since_days=7 日公告/业绩)
    try:
        for i in md.events([symbol], market=market, since_days=7) or []:
            ts = getattr(i, "publish_time", None)
            out["events"].append(
                {
                    "source": getattr(i, "source", ""),
                    "external_id": getattr(i, "external_id", ""),
                    "event_type": getattr(i, "event_type", ""),
                    "title": getattr(i, "title", ""),
                    "publish_time": ts.isoformat() if ts else None,
                    "importance": getattr(i, "importance", 0),
                    "url": getattr(i, "url", ""),
                }
            )
        out["events"].sort(
            key=lambda r: r["publish_time"] or "", reverse=True
        )
    except Exception as e:
        logger.warning(f"基本面明细-事件[{symbol}]查询失败: {e}")

    return out


@router.get("/fundamentals-detail/{symbol}")
async def fundamentals_detail_proxy(
    symbol: str,
    market: str = Query("CN", description="市场"),
    dt_days: int = Query(10, ge=1, le=30, description="龙虎榜回溯天数(自然日)"),
):
    """个股基本面明细合并端点: 龙虎榜/融资融券/股东户数/分红/事件日历。

    每类独立容错, 无数据返回空数组; 单类 vendor 失败不影响其余四类。
    key 来自「设置→接口Key」配置的 data_sources(type=dragon_tiger/margin/...), 实时生效。
    """
    try:
        return fetch_fundamentals_detail(symbol, market=market, dt_days=dt_days)
    except Exception as e:
        logger.warning(f"基本面明细代理失败 [{symbol}]: {e}")
        raise HTTPException(502, f"数据源调用失败: {e}")


# ──────────────── 首页 Dashboard: 东财异动池 + 同花顺热榜 ────────────────


@router.get("/anomalies")
async def anomalies_proxy(
    limit: int = Query(20, ge=1, le=50, description="返回条数(默认20, 最大50)"),
):
    """东财异动池(交易所「严重异常波动」口径), 供首页 Dashboard 直接调用。

    复用 marketdata 的 EmAnomalyVendor(与对话工具 get_market_anomalies 同源),
    但返回结构化 JSON 数组而非文本。同步 vendor 放线程池执行, 不阻塞事件循环;
    无数据返回空数组, vendor 失败返回 502。
    """
    try:
        import asyncio
        from marketdata.vendors.em_anomaly import EmAnomalyVendor

        vendor = EmAnomalyVendor()
        items = await asyncio.to_thread(vendor.fetch, [], {"page_size": limit})
        return [
            {
                "symbol": getattr(it, "symbol", ""),
                "name": getattr(it, "name", ""),
                "market": getattr(it, "market", ""),
                "change_pct": getattr(it, "change_pct", None),
                "deviation": getattr(it, "deviation", None),
                "days": getattr(it, "days", None),
                "rule_code": getattr(it, "rule_code", 0),
                "rule": getattr(it, "rule", "") or "",
                "is_today": bool(getattr(it, "is_today", False)),
                "trade_date": getattr(it, "trade_date", ""),
            }
            for it in (items or [])
        ]
    except Exception as e:
        logger.warning(f"东财异动池代理失败: {e}")
        raise HTTPException(502, f"数据源调用失败: {e}")


@router.get("/hot-stocks")
async def hot_stocks_proxy(
    period: str = Query("hour", description="热榜周期: hour(小时榜,默认)/day(日榜)"),
    limit: int = Query(20, ge=1, le=50, description="返回条数(默认20, 最大50)"),
):
    """同花顺热榜(小时榜/日榜), 供首页 Dashboard 直接调用。

    复用 marketdata 的 ThsHotListVendor(与对话工具 get_hot_stocks 同源),
    返回结构化 JSON 数组(排名/代码/名称/涨跌幅/热度/概念标签/AI归因 analyse)。
    同步 vendor 放线程池执行, 不阻塞事件循环; 无数据返回空数组, vendor 失败返回 502。
    """
    period = (period or "hour").strip().lower()
    if period not in ("hour", "day"):
        period = "hour"
    try:
        import asyncio
        from marketdata.vendors.ths_hot import ThsHotListVendor

        vendor = ThsHotListVendor()
        items = await asyncio.to_thread(
            vendor.fetch, [], {"period": period, "limit": limit}
        )
        return [
            {
                "rank": getattr(it, "rank", 0) or 0,
                "symbol": getattr(it, "symbol", ""),
                "name": getattr(it, "name", ""),
                "market": getattr(it, "market", ""),
                "change_pct": getattr(it, "change_pct", None),
                "heat": getattr(it, "heat", None),
                "concepts": list(getattr(it, "concepts", ()) or ()),
                "reason": (getattr(it, "reason", "") or "").strip(),  # AI 归因(analyse)
            }
            for it in (items or [])
        ]
    except Exception as e:
        logger.warning(f"同花顺热榜代理失败 [{period}]: {e}")
        raise HTTPException(502, f"数据源调用失败: {e}")


# ──────────── 全市场涨跌幅 9 档分桶(v0.4.7) ────────────
# 数据源: 东财 push2 clist 全 A 股列表(沪深京A), 字段 f2=最新价(元), f3=涨跌幅%。
# 仅一次性 HTTP 拉一页拿到 ~5000 行即可覆盖全 A; 加 60s biz_cache 防止高频轮询撞东财限流。
_BREADTH_CACHE_KEY = "breadth:distribution:v1"
_BREADTH_CACHE_TTL = 60

# 9 档分桶定义(从弱到强, 与涨停/跌停并列两极)
_BUCKET_BOUNDS = [
    (-10.0, -9.5, "跌停"),
    (-9.5, -5.0, "<-5%"),
    (-5.0, -3.0, "-5~-3%"),
    (-3.0, -1.0, "-3~-1%"),
    (-1.0, 1.0, "-1~1%"),
    (1.0, 3.0, "1~3%"),
    (3.0, 5.0, "3~5%"),
    (5.0, 9.5, ">5%"),
    (9.5, 10.0, "涨停"),
]

# 涨跌幅近似涨停/跌停阈值(ST 5% / 普通 10%), 取 9.5 作为普通股的"准涨停"分界。
# 真涨停识别: |pct - 10| < 0.05(或 ST: |pct - 5| < 0.05), 边界更稳。
_LIMIT_UP_TOLERANCE = 0.2
_LIMIT_DOWN_TOLERANCE = 0.2


def _classify_bucket(pct: float | None) -> str | None:
    """根据涨跌幅(%)映射到 9 档之一; None/异常返回 None(不计入总数)。"""
    if pct is None:
        return None
    try:
        p = float(pct)
    except (TypeError, ValueError):
        return None
    # 准涨停/准跌停用绝对阈值, 其他用区间
    if p >= 9.8:  # 普通股涨停≈10
        return "涨停"
    if p <= -9.8:  # 普通股跌停≈-10
        return "跌停"
    for lo, hi, label in _BUCKET_BOUNDS:
        if lo <= p < hi:
            return label
    # 极小概率的 p >= 10 或 p < -10(异常), 归到 涨停 / 跌停
    if p >= 10.0:
        return "涨停"
    return "跌停"


def _fetch_breadth_change_pcts() -> list[float]:
    """拉全 A 股涨跌幅(%)数组(v0.4.7.1: 新浪主源, 东财兜底)。

    新浪 Market_Center.getHQNodeData 生产实测可达(东财 push2 clist 在生产云 IP 断连),
    每页 80 只 × 分页拉满(~5400 只, 约 68 页, 每页间隔 60ms 防限流);
    新浪失败回落东财 push2 clist 单页 pz=5000。失败/超时抛异常(由调用方兜底)。
    """
    import time as _time

    import httpx

    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

    def _fetch_sina() -> list[float]:
        out: list[float] = []
        with httpx.Client(timeout=8.0, follow_redirects=True, headers=headers) as client:
            for page in range(1, 80):  # 上限保护: 79页×80 ≈ 6300 > 全A
                resp = client.get(
                    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                    "Market_Center.getHQNodeData",
                    params={
                        "page": str(page), "num": "80", "sort": "changepercent",
                        "asc": "0", "node": "hs_a",
                    },
                )
                rows = resp.json()
                if not rows:
                    break
                for it in rows:
                    try:
                        v = it.get("changepercent")
                        if v is not None and v != "":
                            out.append(float(v))
                    except (TypeError, ValueError):
                        continue
                _time.sleep(0.06)
        return out

    def _fetch_eastmoney() -> list[float]:
        # v0.4.7.3: push2delay 域每页上限 100 条 → 分页拉全A(total~12365 含京市,
        # 上限 140 页保护); 涨跌分布是统计图, 15 分钟延迟无影响
        url = "https://push2delay.eastmoney.com/api/qt/clist/get"
        em_headers = {**headers, "Referer": "https://quote.eastmoney.com/"}
        out: list[float] = []

        def _parse(diff: list) -> None:
            for item in diff:
                # f3 单位是 %(东财惯例, 非小数), 直接拿来用
                try:
                    v = item.get("f3")
                    if v is not None:
                        out.append(float(v))
                except (TypeError, ValueError):
                    continue

        with httpx.Client(timeout=8.0, follow_redirects=True, headers=em_headers) as client:
            total = None
            for page in range(1, 141):
                resp = client.get(url, params={
                    "pn": str(page), "pz": "100", "po": "1", "np": "1",
                    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    "fltt": "2", "invt": "2", "fid": "f3",
                    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81",
                    "fields": "f2,f3",
                })
                data = (resp.json() or {}).get("data") or {}
                if total is None:
                    total = data.get("total") or 0
                diff = data.get("diff") or []
                _parse(diff)
                if len(diff) < 100 or (total and page * 100 >= total):
                    break
        return out

    # 新浪主源(生产可达); 空结果或失败 → 东财兜底; 都挂才抛
    try:
        sina = _fetch_sina()
        if len(sina) >= 1000:  # 合理下限: 全A应>5000
            return sina
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[breadth] 新浪拉取失败: {e!r}, 回落东财")
    return _fetch_eastmoney()


@router.get("/breadth-distribution")
async def breadth_distribution():
    """全市场 A 股涨跌幅 9 档分桶(60s biz_cache)。

    返回格式:
      [{"bucket": "跌停", "count": n}, {"bucket": "<-5%", "count": n}, ...]
    数据缺失: 返回全 0 计数 + note(明示数据源不可用)。
    """
    def _compute() -> dict:
        try:
            pcts = _fetch_breadth_change_pcts()
        except Exception as e:
            logger.warning(f"breadth-distribution 数据源失败: {e}")
            return {
                "count": 0,
                "total": 0,
                "items": [{"bucket": b[2], "count": 0} for b in _BUCKET_BOUNDS],
                "note": f"数据源不可用: {e}",
            }
        # 分桶
        buckets: dict[str, int] = {b[2]: 0 for b in _BUCKET_BOUNDS}
        valid = 0
        for pct in pcts:
            label = _classify_bucket(pct)
            if label is None:
                continue
            valid += 1
            buckets[label] = buckets.get(label, 0) + 1
        items = [{"bucket": b[2], "count": buckets.get(b[2], 0)} for b in _BUCKET_BOUNDS]
        return {
            "count": valid,
            "total": len(pcts),
            "items": items,
            "note": "" if valid else "数据源返回为空(可能非交易日)",
        }

    cached = biz_cache.get_or_fetch(_BREADTH_CACHE_KEY, ttl=_BREADTH_CACHE_TTL, fetch=_compute)
    return cached
