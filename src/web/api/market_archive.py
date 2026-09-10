"""数据落库面查询 API(批次3 查询侧收口, 2026-09-10 28号)。

批次2 落库的四张公共表 + 存量龙虎榜, 只读:
GET /overview                 各表行数+最新 trade_date(新鲜度) + Redis 缓存命中/未命中
GET /quote-snapshots          分钟快照(分时回放底座): symbol+date 必填, ≤2000 行
GET /auction-snapshots        竞价快照: date 必填(当日唯一), 可选 symbol
GET /chip-daily               筹码日频: date 必填, 可选 symbol
GET /dragon-tiger             龙虎榜: date 或 symbol 至少一个, 可选 days 范围

红线: 无数据 → 空 items 不编造; 参数在边界校验(400); 全部只读不触发回源。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from sqlalchemy import text

logger = logging.getLogger(__name__)

router = APIRouter()

_DATE_LEN = 8


def _norm_date(d: str | None, field: str = "date") -> str:
    if not d:
        raise HTTPException(status_code=400, detail=f"{field} 必填(YYYYMMDD)")
    d = d.strip().replace("-", "")
    if len(d) != _DATE_LEN or not d.isdigit():
        raise HTTPException(status_code=400, detail=f"{field} 须为 YYYYMMDD: {d}")
    return d


def _today() -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


def _fetch_rows(sql: str, params: dict) -> list[dict]:
    from src.web.database import SessionLocal

    db = SessionLocal()
    try:
        return [dict(r._mapping) for r in db.execute(text(sql), params).fetchall()]
    finally:
        db.close()


def _redis_stats() -> dict:
    """缓存命中率(Redis INFO stats); Redis 不可用 → 全 None, 不伪装。"""
    try:
        import os

        import redis

        url = (os.getenv("REDIS_URL") or "").strip()
        if not url:
            return {"keyspace_hits": None, "keyspace_misses": None, "hit_rate": None}
        cli = redis.Redis.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5, decode_responses=True)
        info = cli.info("stats")
        hits = info.get("keyspace_hits")
        misses = info.get("keyspace_misses")
        total = (hits or 0) + (misses or 0)
        return {
            "keyspace_hits": hits,
            "keyspace_misses": misses,
            "hit_rate": round(hits / total, 4) if total else None,
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("Redis 统计读取失败: %s", e)
        return {"keyspace_hits": None, "keyspace_misses": None, "hit_rate": None}


@router.get("/overview")
def overview():
    """各落库表新鲜度(行数+最新日期) + 缓存命中率。表不存在按 0 行/无数据计。"""
    tables = [
        "quote_snapshots",
        "auction_snapshots",
        "chip_daily",
        "dragon_tiger_events",
        "l2_ticks",
        "klines",
    ]
    items = []
    for name in tables:
        try:
            rows = _fetch_rows(
                f"SELECT COUNT(*) AS n, COALESCE(MIN(trade_date), '') AS min_d, "
                f"COALESCE(MAX(trade_date), '') AS max_d FROM {name}",
                {},
            )
            items.append(
                {
                    "table": name,
                    "rows": rows[0]["n"] or 0,
                    "earliest_date": rows[0]["min_d"],
                    "latest_date": rows[0]["max_d"],
                }
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("新鲜度统计 %s 失败: %s", name, e)
            items.append(
                {"table": name, "rows": None, "earliest_date": None, "latest_date": None}
            )
    return {"tables": items, "redis_cache": _redis_stats(), "note": "新鲜度=表内最新 trade_date; None=统计失败不伪装"}


@router.get("/quote-snapshots")
def quote_snapshots(symbol: str, date: str | None = None, market: str = "CN", limit: int = 1500):
    """某标的某交易日的分钟快照序列(ts 升序, 分时回放数据底座)。"""
    d = _norm_date(date or _today())
    sym = symbol.strip()
    if not sym:
        raise HTTPException(status_code=400, detail="symbol 必填")
    limit = max(min(limit, 2000), 1)
    rows = _fetch_rows(
        "SELECT trade_date, ts, symbol, market, name, last, open, high, low, prev_close,"
        " change_pct, volume, turnover, turnover_rate, volume_ratio, circ_mv, source"
        f" FROM quote_snapshots WHERE trade_date = :d AND symbol = :sym AND market = :mkt"
        " ORDER BY ts ASC LIMIT :lim",
        {"d": d, "sym": sym, "mkt": market.strip() or "CN", "lim": limit},
    )
    return {
        "symbol": sym,
        "market": market,
        "date": d,
        "count": len(rows),
        "note": "1 分钟桶; 空=当日无采集(非交易时段/标的不在自选∪持仓)" if not rows else "",
        "items": rows,
    }


@router.get("/auction-snapshots")
def auction_snapshots(date: str | None = None, symbol: str | None = None, limit: int = 2000):
    """某交易日全市场竞价快照(当日唯一, 批次2 落库)。"""
    d = _norm_date(date or _today())
    limit = max(min(limit, 5000), 1)
    params: dict = {"d": d, "lim": limit}
    where = "WHERE trade_date = :d"
    if symbol and symbol.strip():
        where += " AND symbol = :sym"
        params["sym"] = symbol.strip()
    rows = _fetch_rows(
        f"SELECT * FROM auction_snapshots {where} ORDER BY symbol ASC LIMIT :lim", params
    )
    return {
        "date": d,
        "count": len(rows),
        "note": "竞价快照当日唯一; 空=当日未采集" if not rows else "",
        "items": rows,
    }


@router.get("/chip-daily")
def chip_daily(date: str | None = None, symbol: str | None = None, limit: int = 2000):
    """某交易日筹码分布日频数据(写穿落库, 批次2)。"""
    d = _norm_date(date or _today())
    limit = max(min(limit, 5000), 1)
    params: dict = {"d": d, "lim": limit}
    where = "WHERE trade_date = :d"
    if symbol and symbol.strip():
        where += " AND symbol = :sym"
        params["sym"] = symbol.strip()
    rows = _fetch_rows(
        f"SELECT * FROM chip_daily {where} ORDER BY symbol ASC LIMIT :lim", params
    )
    return {
        "date": d,
        "count": len(rows),
        "note": "筹码日频(计算写穿落库); 空=当日未采集" if not rows else "",
        "items": rows,
    }


@router.get("/dragon-tiger")
def dragon_tiger(date: str | None = None, symbol: str | None = None, days: int = 0, limit: int = 2000):
    """龙虎榜明细: date=某日全榜 / symbol=某股近 days 日; 至少给一个。"""
    if not date and not (symbol and symbol.strip()):
        raise HTTPException(status_code=400, detail="date 与 symbol 至少一个")
    limit = max(min(limit, 5000), 1)
    params: dict = {"lim": limit}
    where_parts = []
    if date:
        where_parts.append("trade_date = :d")
        params["d"] = _norm_date(date)
    if symbol and symbol.strip():
        sym = symbol.strip()
        where_parts.append("symbol = :sym")
        params["sym"] = sym
        if days and days > 0:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo

            start = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=int(days))).strftime("%Y%m%d")
            where_parts.append("trade_date >= :start")
            params["start"] = start
    rows = _fetch_rows(
        f"SELECT * FROM dragon_tiger_events WHERE {' AND '.join(where_parts)}"
        " ORDER BY trade_date DESC, symbol ASC LIMIT :lim",
        params,
    )
    return {
        "count": len(rows),
        "note": "完整榜单(含 ETF/可转债 6 位码); 空=无覆盖" if not rows else "",
        "items": rows,
    }
