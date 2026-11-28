"""Read the presently visible day bar, factor row and halt evidence together."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import text

from src.db.dialect import is_postgres
from src.db.klines_repo import CN_TRADING_TZ
from src.db.session import get_write_engine

logger = logging.getLogger(__name__)

FACTOR_FIELDS = (
    'momentum_20', 'momentum_60', 'vol_20', 'volume_z20', 'position_120',
    'concentration', 'peak_price', 'rank_momentum_20', 'rank_momentum_60',
    'rank_vol_20', 'rank_volume_z20', 'rank_position_120',
)

# A single statement guarantees one MVCC read snapshot even under READ COMMITTED.
# Same-day priority matches the coverage receipt, with no prior-day fallback.
_READ = text(f"""
    SELECT s.symbol, b.ts AS bar_ts, b.source, b.open, b.high, b.low, b.close,
           b.volume, b.quality_flag, f.id AS factor_id,
           {', '.join(f'f.{key}' for key in FACTOR_FIELDS)},
           h.id AS halt_id, h.start_date AS halt_start, h.end_date AS halt_end, h.source AS halt_source
    FROM unnest(CAST(:symbols AS text[])) AS s(symbol)
    LEFT JOIN LATERAL (
        SELECT ts, source, open, high, low, close, volume, quality_flag FROM klines
        WHERE symbol = s.symbol AND market = 'CN' AND period = '1d' AND adjust = 'qfq'
          AND ts >= :lo AND ts < :hi AND source IN ('tq', 'tencent')
        ORDER BY CASE WHEN source = 'tq' THEN 0 ELSE 1 END, ts LIMIT 1
    ) b ON true
    LEFT JOIN stock_factor_daily f ON f.symbol = s.symbol AND f.trade_date = :compact_date
    LEFT JOIN LATERAL (
        SELECT id, start_date, end_date, source FROM trading_halts
        WHERE symbol = s.symbol AND market = 'CN' AND start_date <= :iso_date
          AND (end_date IS NULL OR end_date = '' OR end_date >= :iso_date)
        ORDER BY start_date DESC, id DESC LIMIT 1
    ) h ON true
""")


def load_stock_observations(day: date, symbols: list[str]) -> dict[str, dict[str, Any]] | None:
    """None = unsupported backend, not evidence of zero observed symbols."""
    if not is_postgres():
        return None
    start = datetime.combine(day, time.min, CN_TRADING_TZ)
    params = dict(symbols=symbols, lo=start, hi=start + timedelta(days=1),
                  iso_date=day.isoformat(), compact_date=day.strftime('%Y%m%d'))
    with get_write_engine().begin() as conn:
        conn.execute(text('SET TRANSACTION READ ONLY'))
        # Three LATERAL probes per symbol at the same 6000-symbol scale as the coverage query.
        conn.execute(text("SET LOCAL statement_timeout = '15s'"))
        with conn.execute(_READ, params) as result:
            rows = {}
            for row in result.mappings():
                symbol = row['symbol']
                if symbol in rows:
                    logger.warning('逐股盘后证据重复行: date=%s symbol=%s', day, symbol)
                    raise ValueError('ambiguous stock evidence')
                rows[symbol] = dict(row)
            return rows
