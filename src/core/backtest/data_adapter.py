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
    B4.5(2026-09-10): 查库逻辑下沉 `src/db/klines_repo.py`, 本模块不再依赖 web 层
    (core 反向依赖收敛, 见 tests/test_w41_core_web_dependency.py)。
    """
    # 1. 查 PG klines 表(qfq 分区; 2026-09-08 风险方案1.2/B1: 复权维度入列,
    #    回测序列必须与主源同为前复权, 严禁混入 none 原始价)
    from src.db.klines_repo import load_qfq_bars

    rows = load_qfq_bars(symbol, market, days)
    if rows:
        # 库里有数据就直接用(用户传 days 只是 hint, 实际取所有行)
        return [
            PriceBar(
                date=r["date"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
            )
            for r in rows
        ]

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
