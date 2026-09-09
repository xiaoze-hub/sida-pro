"""除权除息因子(B1.4/KI-040)。

存"不复权价 + 因子"是消除复权污染的正道: 前复权价会随新除权事件被追溯重算,
历史序列因此含未来信息(见 docs/research/K线复权污染勘查_20260907.md)。
本模块提供因子读写与按 as-of 的折算函数; 存储路径切换(reader 改用 raw+因子)
属独立迁移, 另行实施。
"""

from __future__ import annotations

import logging

from src.web.database import SessionLocal
from src.web.models import AdjFactor

logger = logging.getLogger(__name__)


def upsert_adj_factor(
    db, *, symbol: str, ex_date: str, ratio: float, market: str = "CN", source: str = "manual"
) -> int:
    """按 (symbol, market, ex_date) 幂等写因子; 返回 1/0。"""
    sym = str(symbol or "").strip()
    ex = str(ex_date or "")[:10]
    try:
        r = float(ratio)
    except Exception:
        return 0
    if not sym or not ex or r <= 0:
        return 0
    mkt = str(market or "CN").strip().upper() or "CN"
    row = (
        db.query(AdjFactor)
        .filter(
            AdjFactor.symbol == sym, AdjFactor.market == mkt, AdjFactor.ex_date == ex
        )
        .first()
    )
    if row is None:
        row = AdjFactor(symbol=sym, market=mkt, ex_date=ex)
        db.add(row)
    row.ratio = r
    row.source = source
    return 1


def load_adj_factors(symbol: str, *, market: str | None = None, db=None) -> list[dict]:
    """列出某标的的除权因子(按 ex_date 升序)。"""
    own = db is None
    db = db or SessionLocal()
    try:
        q = db.query(AdjFactor).filter(AdjFactor.symbol == str(symbol or "").strip())
        if market:
            q = q.filter(AdjFactor.market == str(market).upper())
        return [
            {"symbol": r.symbol, "market": r.market, "ex_date": r.ex_date,
             "ratio": float(r.ratio), "source": r.source or ""}
            for r in q.order_by(AdjFactor.ex_date.asc()).all()
        ]
    finally:
        if own:
            db.close()


def adjust_close(raw_close: float, on_date: str, factors: list[dict]) -> float:
    """把 on_date 的不复权收盘价折算到**最新基准**(前复权)。

    只使用 ex_date > on_date 的因子 —— 除权日之前的价格才需要向下折算;
    as-of 语义天然成立: 未来新增的除权事件会让历史价继续下调(与 qfq 一致),
    但调用方若只取 <= as_of 的因子即可得到"当时可见"的序列。
    """
    d = str(on_date or "")[:10]
    px = float(raw_close)
    for f in sorted(factors, key=lambda x: str(x.get("ex_date") or "")):
        if str(f.get("ex_date") or "") > d:
            px *= float(f.get("ratio") or 1.0)
    return px


def to_qfq_series(raw_bars: list[dict], factors: list[dict]) -> list[dict]:
    """不复权 bars → 前复权 bars(open/high/low/close 同乘累计因子)。

    raw_bars: [{date, open, high, low, close, volume?}]
    """
    out: list[dict] = []
    for b in raw_bars:
        d = str(b.get("date") or "")[:10]
        adj = 1.0
        for f in sorted(factors, key=lambda x: str(x.get("ex_date") or "")):
            if str(f.get("ex_date") or "") > d:
                adj *= float(f.get("ratio") or 1.0)
        out.append(
            {
                **b,
                "date": d,
                "open": float(b.get("open") or 0.0) * adj,
                "high": float(b.get("high") or 0.0) * adj,
                "low": float(b.get("low") or 0.0) * adj,
                "close": float(b.get("close") or 0.0) * adj,
                "adj_factor": round(adj, 10),
            }
        )
    return out
