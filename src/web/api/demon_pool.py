"""妖股池 API(批次B, 2026-09-06 28号)。

GET  ""            全市场妖股池(近一年涨停事件 → 六维评分, TopN)
GET  /{symbol}     单只股评分明细(六维分项+flags)
POST /backfill     手动触发回填(运维; 默认 symbols=None 全市场)

红线: 无事件 → 空 list 不编造; 题材/龙虎榜维度未接入 → flags 标注缺数据。
池缓存 300s(评分纯计算, 回填后才变化, 无需更长)。
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

logger = logging.getLogger(__name__)

router = APIRouter()

_CACHE: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 300.0


def _pool_from_db(topn: int = 50, min_events: int = 3) -> list[dict]:
    from src.core.demon_score import demon_score_from_events
    from src.web.database import SessionLocal

    cutoff = _year_ago()
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT trade_date, symbol, name, limit_price, open_price, high_price, low_price,"
                " close_price, touched, is_sealed_close, one_way, open_count, first_time,"
                " max_seal_amount, limit_days, circ_mv FROM limit_up_events"
                " WHERE trade_date >= :cutoff ORDER BY symbol, trade_date"
            ),
            {"cutoff": cutoff},
        ).fetchall()
    finally:
        db.close()
    by_symbol: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r._mapping)
        by_symbol.setdefault(d["symbol"], []).append(d)
    out = []
    for symbol, events in by_symbol.items():
        if len(events) < min_events:
            continue
        name = next((e.get("name") for e in events if e.get("name")), "")
        score = demon_score_from_events(events, circ_mv=None)
        score["symbol"] = symbol
        score["name"] = name
        score["last_event_date"] = str(events[-1].get("trade_date"))
        out.append(score)
    out.sort(key=lambda x: -(x.get("total") or 0))
    return out[:topn]


def _year_ago() -> str:
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    return (now - timedelta(days=365)).strftime("%Y%m%d")


def top_demon_symbols(n: int = 20, min_events: int = 5) -> set[str]:
    """妖股先锋组: 评分 TopN 的代码集合(埋伏评分传导维加成用)。

    独立于路由缓存(直接查), 失败返回空集合(不阻塞盘前主链路)。
    """
    try:
        pool = _pool_from_db(topn=max(n, 1), min_events=min_events)
        return {p["symbol"] for p in pool if p.get("symbol")}
    except Exception as e:  # noqa: BLE001
        logger.warning("先锋组查询失败: %s", e)
        return set()


@router.get("")
def demon_pool(topn: int = 50, refresh: int = 0):
    """妖股池 TopN。min_events=3 起评(近一年≥3 次涨停才有统计意义)。"""
    now = time.monotonic()
    if refresh or _CACHE["data"] is None or now - _CACHE["ts"] > _CACHE_TTL:
        try:
            _CACHE["data"] = _pool_from_db(topn=max(min(topn, 200), 1))
            _CACHE["ts"] = now
        except Exception as e:  # noqa: BLE001
            logger.warning("妖股池计算失败: %s", e)
            raise HTTPException(status_code=500, detail=f"demon-pool failed: {e}")
    return {
        "count": len(_CACHE["data"]),
        "note": "MVP 满分 75+5(题材/龙虎榜维度 wencai 未接入, flags 标注); 权重由回测校准",
        "items": _CACHE["data"][: max(min(topn, 200), 1)],
    }


@router.get("/{symbol}")
def demon_detail(symbol: str):
    try:
        from src.core.demon_score import demon_score_from_events
        from src.core.limit_up_backfill import get_events_window

        events = get_events_window(symbol.strip())
        return {"symbol": symbol, "score": demon_score_from_events(events)}
    except Exception as e:  # noqa: BLE001
        logger.warning("妖股明细 %s 失败: %s", symbol, e)
        raise HTTPException(status_code=500, detail=f"demon detail failed: {e}")


@router.post("/backfill")
def trigger_backfill(symbols: list[str] | None = None, max_stocks: int = 0):
    """手动触发回填(全市场约 3-5 分钟, 走 Engine 主备链路)。"""
    try:
        from src.core.limit_up_backfill import backfill_all

        return backfill_all(symbols=symbols, max_stocks=max_stocks)
    except Exception as e:  # noqa: BLE001
        logger.warning("妖股池回填触发失败: %s", e)
        raise HTTPException(status_code=500, detail=f"backfill failed: {e}")
