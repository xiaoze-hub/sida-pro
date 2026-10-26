# -*- coding: utf-8 -*-
"""I-8 分价博弈矩阵 API(2026-10-18)。

`GET  /api/market/price-distribution/{symbol}`          —— 矩阵主接口(读库优先 → 回源 → 落库)
`GET  /api/market/price-distribution/{symbol}/history`  —— 日终归档序列(只读库, 供回测)
`POST /api/market/price-distribution/{symbol}/archive`  —— 当日定稿归档(收盘 cron / 手动)

挂载前缀 `/api/market`(与 market_phase 的 `/phase`、cancel-profile 共存)。

为什么把读穿透放在 core 而不是路由里: 路由只做参数校验与线程池调度, 缓存/落库策略
(何时读库、何时回源、失败如何 stale)属于数据层, 换 API 框架/加 cron 都要复用同一套
(`src/core/price_distribution_store.py`)。

降级诚实: 上游不可用 → HTTP 200 + `available=false` + `reason` 原文;
有旧快照时出旧数据并标 `cache.stale=true` —— 绝不用 0 或估算值糊过去。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

from src.core.price_distribution_analyzer import DEFAULT_LIMIT, MIN_BINS
from src.core.price_distribution_store import (
    archive_daily,
    get_or_refresh,
    load_daily_history,
)
from src.db.session import SessionLocal

logger = logging.getLogger(__name__)

router = APIRouter()


def _degraded(reason: str, symbol: str, market: str) -> Dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "symbol": symbol,
        "market": market,
        "bins": [],
        "support_levels": [],
        "resistance_levels": [],
        "iceberg_signals": [],
        "summary": [],
        "cache": {"hit": False, "source": "none", "stale": False, "reason": reason},
    }


@router.get("/price-distribution/{symbol}")
async def get_price_distribution(
    symbol: str,
    market: str = Query("CN", description="市场, 仅 A 股(腾讯分价表)支持"),
    limit: int = Query(DEFAULT_LIMIT, ge=MIN_BINS, le=500, description="取成交量最大的前 N 档"),
    refresh: bool = Query(False, description="跳过缓存强制回源(仍会落库)"),
    persist: bool = Query(True, description="是否落库(默认写; 只读副本场景可关)"),
    ttl_s: Optional[int] = Query(None, ge=0, le=3600, description="缓存 TTL 覆盖(秒)"),
) -> Dict[str, Any]:
    """个股微观分价博弈矩阵: 逐档多空力量 + 托举位/抛压带/拆单信号。

    缓存语义: 盘中 TTL 15s(腾讯分钟级更新), 非交易时段 600s(数据冻结);
    `refresh=true` 直连上游。响应 `cache` 字段显式说明本次是 DB 还是现拉。
    """
    sym = (symbol or "").strip()
    if not sym:
        return _degraded("缺少标的代码", sym, market)

    try:
        out = await asyncio.to_thread(
            get_or_refresh,
            sym,
            market,
            limit=limit,
            ttl_s=ttl_s,
            refresh=refresh,
            persist=persist,
        )
    except Exception as e:  # noqa: BLE001 - 任意异常降级为可读原因, 不外抛 500
        logger.warning(f"分价矩阵[{sym}]异常: {e}")
        return _degraded(f"分价矩阵不可用: {e}", sym, market)
    return out


@router.get("/price-distribution/{symbol}/history")
async def get_price_distribution_history(
    symbol: str,
    market: str = Query("CN", description="市场, 仅 A 股"),
    days: int = Query(60, ge=1, le=250, description="最多取多少个交易日"),
) -> Dict[str, Any]:
    """日终归档序列(升序)。**不回源** —— 分价表只给当日, 历史只能来自本库。"""
    sym = (symbol or "").strip()
    if not sym:
        return {"available": False, "reason": "缺少标的代码", "symbol": sym, "items": []}

    def _load() -> list:
        with SessionLocal() as session:
            return load_daily_history(session, sym, market, days=days)

    try:
        items = await asyncio.to_thread(_load)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"分价归档[{sym}]读取失败: {e}")
        return {
            "available": False,
            "reason": f"分价归档读取失败: {e}",
            "symbol": sym,
            "market": market,
            "items": [],
        }

    return {
        "available": bool(items),
        "reason": None if items else "该标的尚无分价日终归档(收盘归档任务尚未覆盖)",
        "symbol": sym,
        "market": market,
        "days": days,
        "count": len(items),
        "items": items,
    }


@router.post("/price-distribution/{symbol}/archive")
async def post_price_distribution_archive(
    symbol: str,
    market: str = Query("CN", description="市场, 仅 A 股"),
    limit: int = Query(DEFAULT_LIMIT, ge=MIN_BINS, le=500),
    refresh: bool = Query(True, description="归档前是否强制回源(默认是, 拿到最新定稿)"),
) -> Dict[str, Any]:
    """把当前分析结果归档为当日定稿(唯一键覆盖)。

    收盘 cron 调一次即当日定稿; 手动调用会先回源(除非 `refresh=false` 用缓存)。
    上游不可用且库里有当日快照时, 归档库里的那份(不新拉也行)。
    """
    sym = (symbol or "").strip()
    if not sym:
        return _degraded("缺少标的代码", sym, market)

    def _run() -> Dict[str, Any]:
        analysis = get_or_refresh(sym, market, limit=limit, refresh=refresh, persist=True)
        if not analysis.get("available"):
            return {
                "archived": False,
                "reason": analysis.get("reason") or "分价数据不可用, 未归档",
                "symbol": sym,
                "trade_date": analysis.get("trade_date"),
            }
        with SessionLocal() as session:
            ok = archive_daily(session, analysis)
        support = analysis.get("support_levels") or []
        resistance = analysis.get("resistance_levels") or []
        return {
            "archived": bool(ok),
            "reason": None if ok else "归档写入被跳过(结果不完整)",
            "symbol": sym,
            "trade_date": analysis.get("trade_date"),
            "snapshot_ts": analysis.get("snapshot_ts"),
            "support_price": support[0].get("price") if support else None,
            "resistance_price": resistance[0].get("price") if resistance else None,
            "iceberg_count": len(analysis.get("iceberg_signals") or []),
        }

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"分价归档[{sym}]失败: {e}")
        return {"archived": False, "reason": f"分价归档失败: {e}", "symbol": sym}
