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


def _norm_dateval(v) -> str | None:
    """DATE/TEXT trade_date → YYYYMMDD; None 原样。"""
    if v is None:
        return None
    if hasattr(v, "strftime"):
        return v.strftime("%Y%m%d")
    s = str(v)
    return s.replace("-", "") if len(s) == 10 and s[4] == "-" else s


# 表 → 日期口径: td=trade_date 列; ts=无 trade_date 用 ts 时间戳(l2_ticks 的 ts 是入库时间)
_TABLE_SPECS = (
    ("quote_snapshots", "td"),
    ("auction_snapshots", "td"),
    ("chip_daily", "td"),
    ("dragon_tiger_events", "td"),
    ("l2_ticks", "ts_est"),
    ("klines", "ts_est"),
)


@router.get("/overview")
def overview():
    """各落库表新鲜度(行数+最早/最新日期) + 缓存命中率。统计失败 → None 不伪装。

    l2_ticks/klines 无 trade_date: 日期取 MIN/MAX(ts)(l2_ticks 的 ts 为入库时间,
    非行情时间), 行数为 PG reltuples 估算(rows_estimated=true)以避免 7900 万行
    精确 COUNT 的 20s 开销。
    """
    from src.web.database import SessionLocal

    items = []
    db = SessionLocal()
    try:
        is_pg = db.bind.dialect.name == "postgresql"
        for name, mode in _TABLE_SPECS:
            try:
                est = False
                if mode == "ts_est":
                    if is_pg:
                        row = db.execute(
                            text("SELECT reltuples::bigint FROM pg_class WHERE relname = :t"),
                            {"t": name},
                        ).fetchone()
                        n = int(row[0]) if row and row[0] and int(row[0]) > 0 else None
                        est = n is not None
                        if n is None:
                            n = db.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar()
                            est = False
                    else:
                        n = db.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar()
                    drow = db.execute(
                        text(f"SELECT MIN(ts) AS a, MAX(ts) AS b FROM {name}")
                    ).fetchone()
                    lo, hi = drow[0], drow[1]
                else:
                    row = db.execute(
                        text(
                            f"SELECT COUNT(*) AS n, MIN(trade_date) AS a, MAX(trade_date) AS b"
                            f" FROM {name}"
                        )
                    ).fetchone()
                    n, est, lo, hi = row[0], False, row[1], row[2]
                items.append(
                    {
                        "table": name,
                        "rows": n,
                        "rows_estimated": est,
                        "earliest_date": _norm_dateval(lo),
                        "latest_date": _norm_dateval(hi),
                    }
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("新鲜度统计 %s 失败: %s", name, e)
                items.append(
                    {
                        "table": name,
                        "rows": None,
                        "rows_estimated": False,
                        "earliest_date": None,
                        "latest_date": None,
                    }
                )
    finally:
        db.close()
    return {
        "tables": items,
        "redis_cache": _redis_stats(),
        "note": "新鲜度=表内最新日期(YYYYMMDD); l2_ticks 为入库时间, 行数带 rows_estimated=true 为估算; None=统计失败不伪装",
    }


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


# ── 通达信龙虎榜(客户端页面缓存, 2026-09-10 老板要求) ─────────────────────────
# 数据来自客户端打开"龙虎榜"页后落地的本地缓存(GBK JSON), 由宿主脚本同步进容器;
# 容器无法反向拉取 → 缓存缺失/过期时 available=false 如实返回, 不编造。
@router.get("/dragon-tiger-tdx")
def dragon_tiger_tdx(limit: int = 500):
    """通达信龙虎榜榜单(净买入/买卖合计/成交占比/陆股通·机构席位数/异动类型)。"""
    from src.core import tdx_lhb

    limit = max(min(int(limit), 2000), 1)
    return tdx_lhb.board(limit=limit)


@router.get("/dragon-tiger-tdx/seats")
def dragon_tiger_tdx_seats(ref_id: str):
    """通达信单一上榜股的席位明细(营业部/买卖额/占比/席位胜率)。

    ref_id = 榜单条目里的 ref_id(客户端页面缓存文件名, 点开该股才会落地;
    缺失 → available=false)。
    """
    from src.core import tdx_lhb

    return tdx_lhb.seats(ref_id)
