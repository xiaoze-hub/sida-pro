"""回测数据适配:PG klines(优先) → KlineCollector(兜底) → PriceBar。

2026-08-17: 引入 TimescaleDB hypertable, K线持久化后, 回测优先查库。
性能: 查库 ~70ms, 联网 ~500ms (单只股 800 天)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from src.db.klines_repo import CN_TRADING_TZ
from src.models.market import MARKETS, MarketCode

logger = logging.getLogger(__name__)
# Latest daily bars retained by the collector fallback; deep history is PG-first.
MAX_FALLBACK_BARS = 800


class HistoryOutsideLookback(ValueError):
    """The collector has history, but none falls inside the lookback."""

    def __init__(self, latest_date: str):
        self.latest_date = latest_date
        super().__init__(f'latest fallback bar before lookback: {latest_date}')


class HistoryAfterWindow(ValueError):
    """The collector only returned bars newer than the requested ceiling."""

    def __init__(self, first_date: str):
        self.first_date = first_date
        super().__init__(f'first fallback bar after window: {first_date}')


def _aware(now: datetime) -> datetime:
    """Reject host-local assumptions: naive input is UTC, never the deployment TZ."""
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now


def history_cutoff_day(days: int, now: datetime) -> date:
    """Inclusive first date label; all markets share the daily-bar storage clock."""
    return (_aware(now).astimezone(timezone.utc) - timedelta(days=days)).astimezone(CN_TRADING_TZ).date()


def last_closed_session(market: str, as_of: datetime) -> date:
    """Upper date label excluding open sessions; not a guarantee of vendor finality."""
    from src.core.trading_calendar import TradingCalendarError, is_trading_day, prev_trading_day

    code = market.upper()
    if code in {'SH', 'SZ', 'BJ'}:
        code = 'CN'
    try:
        meta = MARKETS[MarketCode(code)]
    except ValueError:
        return _aware(as_of).astimezone(CN_TRADING_TZ).date() - timedelta(days=1)
    local = _aware(as_of).astimezone(meta.get_tz())
    day = local.date()
    close = meta.sessions[-1].end
    if meta.code != MarketCode.CN:
        # No HK/US holiday table here. This is only a ceiling; actual bars
        # determine the last observed session. Use exchange-local time (incl. US DST).
        return day if local.time() >= close else day - timedelta(days=1)
    try:
        return day if is_trading_day(day) and local.time() >= close else prev_trading_day(day)
    except TradingCalendarError:
        return day - timedelta(days=1)


def history_window(market: str, days: int, as_of: datetime) -> tuple[date, date]:
    """Inclusive date bounds; this does not reconstruct historical adjustment vintages."""
    end = last_closed_session(market, as_of)
    return min(history_cutoff_day(days, as_of), end), end


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


def load_price_history(symbol: str, market, days: int = 250, *, as_of: datetime | None = None) -> list[PriceBar]:
    """PG 优先；as_of 只限制交易日，不冻结复权版本。

    两条路径都是查询时可见的前复权，可能含后来修订，不能用于证明历史时点可见性。
    指定 as_of 且回退结果只在窗口外时按方向报错；不传则保留旧列表契约。
    """
    # 1. 查 PG klines 表(qfq 分区; 2026-09-08 风险方案1.2/B1: 复权维度入列,
    #    回测序列必须与主源同为前复权, 严禁混入 none 原始价)
    from src.db.klines_repo import load_qfq_bars

    calendar_days = max(1, int(days))
    code = str(getattr(market, 'value', market)).upper()
    bounds = history_window(code, calendar_days, as_of) if as_of is not None else None
    if bounds is None:
        rows = load_qfq_bars(symbol, market, calendar_days)
    else:
        rows = load_qfq_bars(symbol, market, calendar_days, date_cutoff=bounds[0], date_ceiling=bounds[1])
    if rows:
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

    # 2. Collector 的 days 是日 K 根数，不是 PG 的自然日跨度。日期越早，
    # 只放宽 PG 查库范围，不让回退的取数门槛突破固定预算。
    from src.collectors.kline_collector import KlineCollector
    try:
        mc = MarketCode(code)
    except ValueError:
        mc = MarketCode.CN
    fallback_bars = min(calendar_days, MAX_FALLBACK_BARS) if bounds is not None else calendar_days
    try:
        klines = KlineCollector(mc).get_klines(symbol, days=fallback_bars)
    except Exception as e:
        logger.warning(f"[回测] 拉取 {symbol} K线失败: {e}")
        return []
    # 按日历截掉兜底序列中超出回看范围的旧柱；根数上限仍可能缩短左端覆盖。
    converted = from_klines(klines)
    if bounds is None:
        return converted
    cutoff_day, upper_day = bounds[0].isoformat(), bounds[1].isoformat()
    if converted and converted[-1].date < cutoff_day:
        raise HistoryOutsideLookback(converted[-1].date)
    if converted and converted[0].date > upper_day:
        raise HistoryAfterWindow(converted[0].date)
    return [bar for bar in converted if cutoff_day <= bar.date <= upper_day][-fallback_bars:]


def first_index_after(bars: list[PriceBar], date: str) -> int | None:
    """返回第一个 date 严格大于给定日期的 bar 下标(下一交易日,防 look-ahead)。"""
    for i, b in enumerate(bars):
        if b.date > date:
            return i
    return None
