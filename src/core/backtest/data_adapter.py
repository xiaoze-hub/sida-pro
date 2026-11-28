"""回测数据适配:PG klines(优先) → KlineCollector(兜底) → PriceBar。

2026-08-17: 引入 TimescaleDB hypertable, K线持久化后, 回测优先查库。
性能: 查库 ~70ms, 联网 ~500ms (单只股 800 天)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
# Latest daily bars retained by the collector fallback; deep history is PG-first.
MAX_FALLBACK_BARS = 800


class HistoryOutsideLookback(ValueError):
    """The collector has history, but none falls inside the lookback."""

    def __init__(self, latest_date: str):
        self.latest_date = latest_date
        super().__init__(f'latest fallback bar before lookback: {latest_date}')


_BAR_TZ = ZoneInfo('Asia/Shanghai')  # klines are persisted at Shanghai midnight for every market.


def _aware(now: datetime) -> datetime:
    """Reject host-local assumptions: naive input is UTC, never the deployment TZ."""
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now


def history_cutoff_day(market: str, days: int, now: datetime) -> date:
    """Inclusive first date label, anchored to the bar persistence timezone."""
    return (_aware(now).astimezone(timezone.utc) - timedelta(days=days)).astimezone(_BAR_TZ).date()


def last_closed_session(market: str, as_of: datetime) -> date:
    """Latest session whose daily bar is final: before 15:00 Shanghai it is the previous one."""
    from src.core.trading_calendar import TradingCalendarError, is_trading_day, prev_trading_day

    local = _aware(as_of).astimezone(_BAR_TZ)
    day = local.date()
    try:
        if local.time() < time(15, 0):
            # prev_trading_day already walks back to the latest trading day.
            day = prev_trading_day(day)
        elif not is_trading_day(day):
            day = prev_trading_day(day)
    except (TradingCalendarError, ValueError, OverflowError):
        return local.date()
    return day


def history_window(market: str, days: int, as_of: datetime) -> tuple[date, date]:
    """Point-in-time window of final bars: inclusive first label and last closed session."""
    end = last_closed_session(market, as_of)
    return min(history_cutoff_day(market, days, as_of), end), end


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
    """含 as_of 的时点窗口；不传 as_of 时保留原滚动查库与兜底契约。"""
    """PG 优先。指定 as_of 用统一日历窗口，仅有旧柱时抛出 HistoryOutsideLookback。

    不传 as_of 的调用保留滚动 PG 和采集器原契约，不按回测样本裁剪。

    2026-08-17: 引入 TimescaleDB hypertable, K线持久化后, 回测不再每次联网。
    性能: 查库 ~70ms, 联网 ~500ms (单只股 800 天)。
    B4.5(2026-09-10): 查库逻辑下沉 `src/db/klines_repo.py`, 本模块不再依赖 web 层
    (core 反向依赖收敛, 见 tests/test_w41_core_web_dependency.py)。
    """
    # 1. 查 PG klines 表(qfq 分区; 2026-09-08 风险方案1.2/B1: 复权维度入列,
    #    回测序列必须与主源同为前复权, 严禁混入 none 原始价)
    from src.db.klines_repo import load_qfq_bars

    calendar_days = max(1, int(days))
    cutoff, upper = ((history_window(str(getattr(market, 'value', market)), calendar_days, as_of))
                     if as_of is not None else (None, None))
    day_cutoff = cutoff
    rows = (load_qfq_bars(symbol, market, calendar_days, date_cutoff=day_cutoff, date_ceiling=upper)
            if day_cutoff is not None else load_qfq_bars(symbol, market, calendar_days))
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
    from src.models.market import MarketCode

    try:
        mc = market if isinstance(market, MarketCode) else MarketCode(str(market).upper())
    except Exception:
        mc = MarketCode.CN
    fallback_bars = min(calendar_days, MAX_FALLBACK_BARS) if day_cutoff is not None else calendar_days
    try:
        klines = KlineCollector(mc).get_klines(symbol, days=fallback_bars)
    except Exception as e:
        logger.warning(f"[回测] 拉取 {symbol} K线失败: {e}")
        return []
    # 按日历截掉兜底序列中超出回看范围的旧柱；根数上限仍可能缩短左端覆盖。
    converted = from_klines(klines)
    if day_cutoff is None:
        return converted
    assert upper is not None  # history_window always returns both bounds with as_of.
    cutoff_day, upper_day = day_cutoff.isoformat(), upper.isoformat()
    returned = converted[-fallback_bars:]
    in_window = [bar for bar in returned if cutoff_day <= bar.date <= upper_day]
    if returned and not in_window and returned[-1].date < cutoff_day:
        # Only the genuinely-earlier direction is a lookback miss; newer-than-ceiling
        # bars are a window problem the caller reports as history_starts_after_window.
        raise HistoryOutsideLookback(returned[-1].date)
    return in_window


def first_index_after(bars: list[PriceBar], date: str) -> int | None:
    """返回第一个 date 严格大于给定日期的 bar 下标(下一交易日,防 look-ahead)。"""
    for i, b in enumerate(bars):
        if b.date > date:
            return i
    return None
