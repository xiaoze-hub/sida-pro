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
def dragon_tiger(
    date: str | None = None,
    symbol: str | None = None,
    days: int = 0,
    limit: int = 2000,
    market: str | None = None,
):
    """龙虎榜明细: date=某日全榜 / symbol=某股近 days 日; 至少给一个。

    另带 `quality`(仅 date 查询): `dragon_tiger_events` 的**独立**完整性定性 ——
    该表是定时回源落库的独立来源, **没有官方完整清单可核对**, 因此
    `complete=False` / `completeness_claimed=False`, 不声明完整; 有行时
    `severity=caution`(无证据表明残缺), 无行时 `severity=missing`。
    不要把这里的 `count` 读成"完整上榜数"。

    `date` + `symbol` 同时给时, `items`/`count` 仍只含本股, 但 `quality` **始终按该日
    全日实际市场范围**计算(不用过滤后的行做全日定性); 本股当日有没有行另见
    `quality.subject`(`recorded`: True/False/None) —— "本股无记录"与"全日无覆盖"
    是两件事。
    """
    if not date and not (symbol and symbol.strip()):
        raise HTTPException(status_code=400, detail="date 与 symbol 至少一个")
    limit = max(min(limit, 5000), 1)
    params: dict = {"lim": limit}
    where_parts = []
    mkt = (market or "").strip().upper() or None
    sym = (symbol or "").strip() or None
    d: str | None = None
    if date:
        d = _norm_date(date)
        where_parts.append("trade_date = :d")
        params["d"] = d
    if sym:
        where_parts.append("symbol = :sym")
        params["sym"] = sym
        if days and days > 0:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo

            start = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=int(days))).strftime("%Y%m%d")
            where_parts.append("trade_date >= :start")
            params["start"] = start
    if mkt:
        where_parts.append("market = :mkt")
        params["mkt"] = mkt
    rows = _fetch_rows(
        f"SELECT * FROM dragon_tiger_events WHERE {' AND '.join(where_parts)}"
        " ORDER BY trade_date DESC, symbol ASC LIMIT :lim",
        params,
    )
    note = (
        "龙虎榜明细(含 ETF/可转债 6 位码); 空=无覆盖; "
        "无官方完整清单可核对, 不声明完整(见 quality)"
    )
    if sym:
        # 明细按 symbol 过滤, count 只是**本股记录数**; 不能让读侧把它当全日覆盖。
        note += (
            f" 本次按 symbol={sym} 过滤: count 是本股记录数; 空=本股"
            + ("当日" if d else "在该区间")
            + "无记录, 不代表全日无覆盖"
            + ("(全日定性见 quality)" if d else "")
            + "。"
        )
    payload = {"count": len(rows), "note": note, "items": rows}
    if d:
        if sym:
            # 质量必须看**全日实际市场范围**: 用 symbol 过滤后的行做全日定性, 会把
            # 「本股无记录」误判成「全日无快照」(空 rows → status=absent)。
            day_rows = _day_event_rows(d, mkt)
            if day_rows is None:
                payload["quality"] = _quality_unavailable(
                    d, mkt or "CN", "全日范围读取失败(单股过滤结果不作全日定性)", symbol=sym
                )
            else:
                payload["quality"] = _events_quality(d, day_rows, mkt or "CN", symbol=sym)
        else:
            payload["quality"] = _events_quality(d, rows, mkt or "CN")
    return payload


def _day_event_rows(trade_date: str, market: str | None = None) -> list[dict] | None:
    """该日**全市场** `dragon_tiger_events` 的最小列(symbol/market/created_at)。

    只读、按 trade_date 取(命中唯一键 (trade_date, symbol, reason) 的前缀索引),
    不 `SELECT *`、不分页; 读失败 → None(调用方如实说「定性不可用」, 不猜)。
    """
    sql = "SELECT symbol, market, created_at FROM dragon_tiger_events WHERE trade_date = :d"
    params: dict = {"d": trade_date}
    if market:
        sql += " AND market = :mkt"
        params["mkt"] = market
    try:
        return _fetch_rows(sql, params)
    except Exception as e:  # noqa: BLE001 - 表缺失/库不可达 → 定性不可用
        logger.debug("龙虎榜全日范围读取失败(不阻断): %s", e)
        return None


def _daily_symbols(trade_date: str, market: str = "CN") -> list[str] | None:
    """`dragon_tiger_daily` 当日同市场 symbol(仅作交叉提示); 读不到 → None(不猜)。"""
    try:
        rows = _fetch_rows(
            "SELECT symbol FROM dragon_tiger_daily WHERE trade_date = :d AND market = :m",
            {"d": trade_date, "m": market},
        )
        return [str(r.get("symbol")) for r in rows if r.get("symbol")]
    except Exception as e:  # noqa: BLE001 - 表缺失/库不可达 → 交叉提示缺席, 不编造
        logger.debug("龙虎榜 daily 交叉提示读取失败(不阻断): %s", e)
        return None


def _events_quality(
    trade_date: str, rows: list[dict], market: str, *, symbol: str | None = None
) -> dict:
    """`dragon_tiger_events` 的**全日**独立定性(不声明完整); 失败也如实说"不可用"。

    `rows` 必须是该日**全日实际市场范围**(按 symbol 过滤后的行只能说明本股) ——
    单股查询另由 `symbol` 附加 `subject` 视角, 不改变全日 `status`。
    """
    try:
        from src.core import lhb_quality as lq

        same = [
            r for r in rows
            if str(r.get("market") or "CN").strip().upper() == market
        ]
        fw, lw = lq.beijing_write_window([r.get("created_at") for r in same])
        day_syms = [r.get("symbol") for r in same if r.get("symbol")]
        qual = lq.assess_events_date(
            trade_date,
            market,
            event_symbols=day_syms,
            first_write_at=fw,
            last_write_at=lw,
            daily_symbols=_daily_symbols(trade_date, market),
        )
        if len(same) != len(rows):
            # items 未按市场过滤时, 定性只覆盖一个市场 —— 明说, 别让 count 与产量对不上。
            qual["rows_total"] = len(rows)
            qual["rows_in_market"] = len(same)
            qual["market_scope_note"] = (
                f"本次定性仅覆盖 {market} 市场 {len(same)}/{len(rows)} 行; "
                "其余市场未参与定性(不据此推断其残缺或完整)。"
            )
            qual["note"] = f"{qual['note']} {qual['market_scope_note']}"
        if symbol:
            qual["scope"] = "day_market"
            qual["subject"] = lq.assess_symbol_scope(
                symbol, day_symbols=day_syms, day_status=qual["status"]
            )
            qual["note"] = f"{qual['note']} 单股视角: {qual['subject']['note']}"
        return qual
    except Exception as e:  # noqa: BLE001 - 定性是增强, 不拖垮明细读取
        logger.debug("dragon_tiger_events 完整性定性失败(不阻断): %s", e)
        return _quality_unavailable(
            trade_date, market, f"定性异常({type(e).__name__})", symbol=symbol
        )


def _quality_unavailable(
    trade_date: str, market: str, reason: str, *, symbol: str | None = None
) -> dict:
    """定性不可用时的显式降级载荷: 不声明完整, 也不据此声明无上榜。"""
    note = "完整性定性不可用; 不声明该日榜单完整, 也不据此声明无上榜。"
    payload = {
        "table": "dragon_tiger_events",
        "trade_date": trade_date,
        "market": market,
        "status": "unknown",
        "complete": False,
        "completeness_claimed": False,
        "verified_against_official": False,
        "degraded": True,
        "severity": "unknown",
        "label": "定性不可用",
        "note": note,
    }
    if symbol:
        payload["scope"] = "day_market"
        payload["subject"] = {
            "symbol": symbol,
            "recorded": None,
            "day_symbols": None,
            "day_status": "unknown",
            "note": f"本股({symbol})是否在榜不可判定: 全日范围定性未取得。",
        }
        note = f"{note} 单股视角: {payload['subject']['note']}"
    payload["note"] = f"{note} 原因: {reason}。"
    return payload


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


# ── 龙虎榜机构/营业部明细(东财, 全自动定时入库, 2026-09-10) ──────────────────
@router.get("/lhb-institution")
def lhb_institution(date: str | None = None, symbol: str | None = None, days: int = 0, limit: int = 2000):
    """机构买卖统计(含上榜后 1/2/3/5/10 日涨幅): date 或 symbol 至少一个。"""
    if not date and not (symbol and symbol.strip()):
        raise HTTPException(status_code=400, detail="date 与 symbol 至少一个")
    limit = max(min(int(limit), 5000), 1)
    params: dict = {"lim": limit}
    where = []
    if date:
        where.append("trade_date = :d")
        params["d"] = _norm_date(date)
    if symbol and symbol.strip():
        where.append("symbol = :sym")
        params["sym"] = symbol.strip()
        if days and days > 0:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo

            start = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=int(days))).strftime("%Y%m%d")
            where.append("trade_date >= :start")
            params["start"] = start
    rows = _fetch_rows(
        f"SELECT * FROM lhb_institution_daily WHERE {' AND '.join(where)}"
        " ORDER BY trade_date DESC, symbol ASC LIMIT :lim",
        params,
    )
    return {"count": len(rows), "note": "空=无覆盖(非交易日或不含机构上榜)" if not rows else "", "items": rows}


@router.get("/lhb-seats")
def lhb_seats(
    date: str | None = None,
    symbol: str | None = None,
    side: str | None = None,
    days: int = 0,
    limit: int = 2000,
):
    """营业部席位明细: date 或 symbol 至少一个; side=buy|sell 可选。"""
    if not date and not (symbol and symbol.strip()):
        raise HTTPException(status_code=400, detail="date 与 symbol 至少一个")
    limit = max(min(int(limit), 5000), 1)
    if side and side not in ("buy", "sell"):
        raise HTTPException(status_code=400, detail="side 仅支持 buy/sell")
    params: dict = {"lim": limit}
    where = []
    if date:
        where.append("trade_date = :d")
        params["d"] = _norm_date(date)
    if symbol and symbol.strip():
        where.append("symbol = :sym")
        params["sym"] = symbol.strip()
        if days and days > 0:
            from datetime import datetime, timedelta
            from zoneinfo import ZoneInfo

            start = (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=int(days))).strftime("%Y%m%d")
            where.append("trade_date >= :start")
            params["start"] = start
    if side:
        where.append("side = :side")
        params["side"] = side
    rows = _fetch_rows(
        f"SELECT * FROM lhb_seat_details WHERE {' AND '.join(where)}"
        " ORDER BY trade_date DESC, symbol ASC, side ASC, net_amt DESC LIMIT :lim",
        params,
    )
    return {"count": len(rows), "note": "空=无覆盖(非交易日或榜单无营业部明细)" if not rows else "", "items": rows}
