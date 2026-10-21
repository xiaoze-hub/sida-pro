"""因子历史回填（2026-10-21）。

为什么要回填：IC 需要"T 日因子 × T+N 日收益"的横截面序列。如果等每天采集自然累积，
要 25 个交易日才有第一批 IC —— 而因子是**可从历史日线重算**的，没必要等。

做法：每股读一次 250 根日线，然后按最近 N 个交易日逐个截断重算（含 CYQ 集中度与筹码峰），
写入 stock_factor_daily（同一 (symbol, trade_date) 幂等覆盖）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _trading_dates(db, days: int) -> list[str]:
    from sqlalchemy import text

    try:
        with db.get_bind().connect() as conn:
            rows = conn.execute(text(
                "SELECT DISTINCT to_char(ts,'YYYYMMDD') FROM klines "
                "WHERE period='1d' AND market='CN' AND adjust='qfq' "
                "ORDER BY 1 DESC LIMIT :n"
            ), {"n": days}).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.warning("回填取交易日失败: %s", exc)
        return []
    return sorted(str(r[0]) for r in rows if r[0])


def backfill(db: Session, days: int = 30, limit: int | None = None) -> dict:
    from sqlalchemy import text

    from src.collectors.factor_daily import compute_factors
    from src.db.klines_repo import load_qfq_bars
    from src.db.models import StockFactorDaily

    dates = _trading_dates(db, days)
    if not dates:
        return {"written": 0, "reason": "no_dates"}
    try:
        with db.get_bind().connect() as conn:
            rows = conn.execute(text(
                "SELECT DISTINCT symbol FROM klines WHERE period='1d' AND adjust='qfq' "
                "AND market='CN' AND ts >= now() - interval '200 days'"
            )).fetchall()
    except Exception as exc:  # noqa: BLE001
        return {"written": 0, "reason": f"pool_failed:{type(exc).__name__}"}

    symbols = [str(r[0]) for r in rows if r[0]]
    if limit:
        symbols = symbols[:limit]

    written, skipped = 0, 0
    for idx, sym in enumerate(symbols, 1):
        try:
            bars = load_qfq_bars(sym, "CN", days=260)
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        if len(bars) < 60:
            skipped += 1
            continue
        for day in dates:
            use = [b for b in bars if str(b.get("date", "")).replace("-", "") <= day]
            if len(use) < 60:
                continue
            f = compute_factors(use)
            if f["momentum_20"] is None:
                continue
            rec = {"symbol": sym, "trade_date": day, **f}
            obj = db.query(StockFactorDaily).filter_by(symbol=sym, trade_date=day).first()
            if obj is None:
                db.add(StockFactorDaily(**rec))
            else:
                for k, v in rec.items():
                    setattr(obj, k, v)
            written += 1
        if idx % 200 == 0:
            db.commit()
            logger.info("因子回填进度 %d/%d，已写 %d 行", idx, len(symbols), written)
    db.commit()
    logger.info("因子回填完成：%d 行 / %d 只 / %d 个交易日（跳过 %d 只）",
                written, len(symbols), len(dates), skipped)
    return {"written": written, "symbols": len(symbols), "dates": len(dates), "skipped": skipped,
            "range": [dates[0], dates[-1]]}
