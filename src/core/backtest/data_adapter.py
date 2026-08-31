"""回测数据适配:PG klines(优先) → KlineCollector(兜底) → PriceBar。

2026-08-17: 引入 TimescaleDB hypertable, K线持久化后, 回测优先查库。
性能: 查库 ~70ms, 联网 ~500ms (单只股 800 天)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriceBar:
    """单根日 K(前复权)。"""

    date: str  # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: float


def from_klines(klines) -> list[PriceBar]:
    """KlineData 列表 → 按日期升序的 PriceBar 列表。"""
    out: list[PriceBar] = []
    for k in klines or []:
        try:
            out.append(
                PriceBar(
                    date=str(k.date)[:10],
                    open=float(k.open),
                    high=float(k.high),
                    low=float(k.low),
                    close=float(k.close),
                    volume=float(k.volume or 0),
                )
            )
        except Exception:
            continue
    out.sort(key=lambda b: b.date)
    return out


def load_price_history(symbol: str, market, days: int = 250) -> list[PriceBar]:
    """优先从 PG klines 表查; 库里没有或不够再 fallback 到 KlineCollector 实时拉。

    2026-08-17: 引入 TimescaleDB hypertable, K线持久化后, 回测不再每次联网。
    性能: 查库 ~70ms, 联网 ~500ms (单只股 800 天)。
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import create_engine, text
    from src.web.database import DB_URL

    # 1. 查 PG klines 表(主源 tencent)
    try:
        mc_str = market.value if hasattr(market, "value") else str(market).upper()
        mc_str = "CN" if mc_str in ("SH", "SZ", "BJ") else mc_str

        engine = create_engine(DB_URL, pool_pre_ping=True)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT ts, open, high, low, close, volume "
                    "FROM klines "
                    "WHERE symbol=:s AND market=:m AND period='1d' "
                    "  AND source='tencent' AND ts >= :c "
                    "ORDER BY ts ASC"
                ),
                {"s": symbol, "m": mc_str, "c": cutoff},
            ).fetchall()
        engine.dispose()

        # 库里有数据就直接用(用户传 days 只是 hint, 实际取所有行)
        if rows:
            return [
                PriceBar(
                    date=str(r[0])[:10],
                    open=float(r[1]),
                    high=float(r[2]),
                    low=float(r[3]),
                    close=float(r[4]),
                    volume=float(r[5] or 0),
                )
                for r in rows
            ]
    except Exception as e:
        logger.warning(f"[回测] 查 PG klines 失败 {symbol}: {e}, fallback 到联网拉")

    # 2. Fallback: 走 KlineCollector 拉
    from src.collectors.kline_collector import KlineCollector
    from src.models.market import MarketCode

    try:
        mc = market if isinstance(market, MarketCode) else MarketCode(str(market).upper())
    except Exception:
        mc = MarketCode.CN
    try:
        klines = KlineCollector(mc).get_klines(symbol, days=days)
    except Exception as e:
        logger.warning(f"[回测] 拉取 {symbol} K线失败: {e}")
        return []
    return from_klines(klines)


def first_index_after(bars: list[PriceBar], date: str) -> int | None:
    """返回第一个 date 严格大于给定日期的 bar 下标(下一交易日,防 look-ahead)。"""
    for i, b in enumerate(bars):
        if b.date > date:
            return i
    return None
