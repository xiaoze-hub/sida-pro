"""停牌区间(B1.5/KI-040)。

停牌期的正确处理: 数据层表现为"当日无 K 线", 持仓层应显式冻结(不可买卖)。
本模块提供区间读写与按日判定; 回测/研究用 `is_halted` 过滤, 避免把停牌
当作"无波动的持有日"。
"""

from __future__ import annotations

import logging

from src.db.session import SessionLocal
from src.db.models import TradingHalt

logger = logging.getLogger(__name__)


def upsert_halt(
    db,
    *,
    symbol: str,
    market: str = "CN",
    start_date: str,
    end_date: str = "",
    reason: str = "",
    source: str = "manual",
) -> int:
    """按 (symbol, market, start_date) 幂等写入停牌区间; 返回 1/0。"""
    sym = str(symbol or "").strip()
    start = str(start_date or "")[:10]
    if not sym or not start:
        return 0
    mkt = str(market or "CN").strip().upper() or "CN"
    row = (
        db.query(TradingHalt)
        .filter(
            TradingHalt.symbol == sym,
            TradingHalt.market == mkt,
            TradingHalt.start_date == start,
        )
        .first()
    )
    if row is None:
        row = TradingHalt(symbol=sym, market=mkt, start_date=start)
        db.add(row)
    row.end_date = str(end_date or "")[:10]
    row.reason = reason or row.reason or ""
    row.source = source
    return 1


def is_halted(symbol: str, on_date: str, *, market: str | None = None, db=None) -> bool:
    """symbol 在 on_date 是否处于停牌区间。"""
    own = db is None
    db = db or SessionLocal()
    try:
        d = str(on_date or "")[:10]
        if not d:
            return False
        q = db.query(TradingHalt).filter(
            TradingHalt.symbol == str(symbol or "").strip(),
            TradingHalt.start_date <= d,
        )
        if market:
            q = q.filter(TradingHalt.market == str(market).upper())
        for row in q.all():
            end = str(row.end_date or "")
            if not end or end >= d:
                return True
        return False
    finally:
        if own:
            db.close()


def load_halts(symbol: str, *, market: str | None = None, db=None) -> list[dict]:
    """列出某标的的停牌区间(升序)。"""
    own = db is None
    db = db or SessionLocal()
    try:
        q = db.query(TradingHalt).filter(
            TradingHalt.symbol == str(symbol or "").strip()
        )
        if market:
            q = q.filter(TradingHalt.market == str(market).upper())
        return [
            {
                "symbol": r.symbol,
                "market": r.market,
                "start_date": r.start_date,
                "end_date": r.end_date or "",
                "reason": r.reason or "",
            }
            for r in q.order_by(TradingHalt.start_date.asc()).all()
        ]
    finally:
        if own:
            db.close()
