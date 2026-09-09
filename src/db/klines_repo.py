"""klines 数据访问仓库(B4.5/KI-039)。

把"回测取数直连 DB"的逻辑从 `src/core/backtest/data_adapter.py` 下沉到数据层,
让 core 不再 import src.web。仓储层是唯一允许碰 ORM/引擎构造的地方。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _norm_market(market) -> str:
    mc = market.value if hasattr(market, "value") else str(market).upper()
    return "CN" if mc in ("SH", "SZ", "BJ") else mc


def load_qfq_bars(symbol: str, market, days: int = 250) -> list[dict]:
    """按 (symbol, market) 取前复权日 K(升序)。

    返回 [{date, open, high, low, close, volume}] —— 与 core 的 PriceBar 字段对齐,
    由调用方自行映射, 避免数据层依赖 core 的类型。

    查库失败返回 [] (调用方决定是否联网兜底), 不抛异常。
    """
    from sqlalchemy import create_engine, text

    from src.web.database import DB_URL

    try:
        mc_str = _norm_market(market)
        engine = create_engine(DB_URL, pool_pre_ping=True)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT ts, open, high, low, close, volume "
                    "FROM klines "
                    "WHERE symbol=:s AND market=:m AND period='1d' "
                    "  AND source='tencent' AND adjust='qfq' AND ts >= :c "
                    "ORDER BY ts ASC"
                ),
                {"s": symbol, "m": mc_str, "c": cutoff},
            ).fetchall()
        engine.dispose()
        return [
            {
                "date": str(r[0])[:10],
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5] or 0),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[klines_repo] 查 PG klines 失败 {symbol}: {e}")
        return []
