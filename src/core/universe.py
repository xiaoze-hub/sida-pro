"""PIT 股票池快照(B0.6, KI-036)。

回测/研究必须按 `as_of_date` 取池 —— 用"今天的名单"回看历史会引入幸存者偏差
(已退市/已戴帽标的被系统性剔除, 收益与胜率偏高且无法靠调参弥补)。

职责:
- `upsert_universe`: 幂等写某日的池快照;
- `universe_as_of`: 按日期取池(无快照 → 空列表, 调用方显式兜底, 不静默用今天名单);
- `backfill_from_entry_candidates`: 用现有 EntryCandidate 历史快照回填(库内唯一 PIT 来源);
- `backfill_from_stock_table`: 用当前 Stock 名单生成"今天"的池(历史日勿用)。
"""

from __future__ import annotations

import logging
from datetime import date

from src.web.database import SessionLocal
from src.web.models import StockUniverseSnapshot

logger = logging.getLogger(__name__)


def is_st_name(stock_name: str | None) -> bool:
    """名称含 ST/*ST → True(代码无法判 ST, 只能靠名称)。"""
    return "ST" in (stock_name or "").upper()


def upsert_universe(db, *, as_of_date: str, rows, source: str = "backfill") -> int:
    """按 (as_of_date, symbol, market) 幂等写入池快照, 返回写入/更新行数。"""
    as_of = str(as_of_date or "")[:10]
    if not as_of:
        return 0
    existing = {
        (r.symbol, (r.market or "CN")): r
        for r in db.query(StockUniverseSnapshot)
        .filter(StockUniverseSnapshot.as_of_date == as_of)
        .all()
    }
    count = 0
    for item in rows:
        symbol = str(item.get("symbol") or "").strip()
        if not symbol:
            continue
        market = str(item.get("market") or "CN").strip().upper() or "CN"
        row = existing.get((symbol, market))
        if row is None:
            row = StockUniverseSnapshot(as_of_date=as_of, symbol=symbol, market=market)
            db.add(row)
            existing[(symbol, market)] = row
        row.stock_name = item.get("stock_name") or row.stock_name or ""
        row.is_st = bool(item.get("is_st", is_st_name(row.stock_name)))
        row.is_delisted = bool(item.get("is_delisted", False))
        row.list_date = item.get("list_date") or row.list_date or ""
        row.delist_date = item.get("delist_date") or row.delist_date or ""
        row.source = source
        count += 1
    return count


def universe_as_of(as_of_date: str, *, market: str | None = None, include_st: bool = True, db=None) -> list[str]:
    """取 as_of_date 当日的股票池(按日期精确匹配; 无快照 → 空列表)。

    只返回未退市标的; include_st=False 时剔除 ST。
    """
    own = db is None
    db = db or SessionLocal()
    try:
        q = db.query(StockUniverseSnapshot).filter(
            StockUniverseSnapshot.as_of_date == str(as_of_date or "")[:10],
            StockUniverseSnapshot.is_delisted.is_(False),
        )
        if market:
            q = q.filter(StockUniverseSnapshot.market == str(market).upper())
        if not include_st:
            q = q.filter(StockUniverseSnapshot.is_st.is_(False))
        return [r.symbol for r in q.all()]
    finally:
        if own:
            db.close()


def filter_symbols(symbols, as_of_date: str, *, market: str = "CN", db=None) -> list[str]:
    """按 PIT 池过滤标的; 无该日快照 → 原样返回(并记 warning, 调用方知情)。"""
    pit = set(universe_as_of(as_of_date, market=market, db=db))
    if not pit:
        logger.warning("[PIT] %s 无股票池快照, 回退调用方名单(存在幸存者偏差)", as_of_date)
        return [s for s in symbols]
    return [s for s in symbols if str(s or "").strip() in pit]


def backfill_from_entry_candidates(*, market: str | None = None, db=None) -> dict:
    """用 EntryCandidate 的历史快照回填池(每个 snapshot_date 一行一票)。"""
    from src.web.models import EntryCandidate

    own = db is None
    db = db or SessionLocal()
    try:
        q = db.query(
            EntryCandidate.snapshot_date,
            EntryCandidate.stock_symbol,
            EntryCandidate.stock_market,
            EntryCandidate.stock_name,
        ).distinct()
        if market:
            q = q.filter(EntryCandidate.stock_market == str(market).upper())
        grouped: dict[str, list[dict]] = {}
        for snap_date, symbol, mkt, name in q.all():
            d = str(snap_date or "")[:10]
            if not d or not symbol:
                continue
            grouped.setdefault(d, []).append(
                {
                    "symbol": symbol,
                    "market": (mkt or "CN").upper(),
                    "stock_name": name or "",
                    "is_st": is_st_name(name),
                }
            )
        total = 0
        for d, rows in grouped.items():
            total += upsert_universe(db, as_of_date=d, rows=rows, source="entry_candidates")
        db.commit()
        return {"dates": len(grouped), "rows": total}
    finally:
        if own:
            db.close()


def backfill_from_stock_table(*, as_of_date: str | None = None, db=None) -> int:
    """用当前 Stock 名单生成某日池(仅"今天"或兜底用, 历史日勿用)。"""
    from src.web.models import Stock

    own = db is None
    db = db or SessionLocal()
    try:
        d = (as_of_date or date.today().isoformat())[:10]
        rows = [
            {
                "symbol": s.symbol,
                "market": (getattr(s, "market", None) or "CN"),
                "stock_name": s.name or "",
                "is_st": is_st_name(s.name),
            }
            for s in db.query(Stock).all()
            if getattr(s, "symbol", None)
        ]
        n = upsert_universe(db, as_of_date=d, rows=rows, source="stock")
        db.commit()
        return n
    finally:
        if own:
            db.close()
