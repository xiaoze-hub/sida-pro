"""轻量事件式回测内核(纯 Python,无第三方依赖)。

职责:给定信号 + 历史 K 线 → 模拟「信号次日开盘入场、逐日止损/止盈/到期平仓」,
扣 A 股交易成本,产出每笔交易、净值曲线与绩效指标。

设计取舍(Phase 0):
- 入场:信号日之后的**下一交易日开盘价**入场(无未来函数);T+1 起才可平仓(符合 A 股)。
- 平仓(event):逐日检查止损/止盈;同日双触保守判为先止损;达最大持有交易日按收盘平。
- 跳空:开盘已越过止损/止盈则按开盘价成交(gap)。
- 仓位:默认每笔固定名义资金,买 A 股 100 股整数倍(可注入 sizer 供 Phase 1 替换)。
- 净值曲线(B0.1, 2026-09-09):**逐交易日 mark-to-market** —— 现金 + 持仓按当日收盘市值,
  与 metrics.py 的契约(逐日净值序列)一致; 年化/夏普/最大回撤均由此口径计算。

已知限制(涨跌停, B0.2 部分建模):
- ✅ 已建模: 一字涨停/跌停(全天封死)不可成交, 顺延到下一可成交日(见 _buyable/_sellable);
  ST 涨跌停幅度 5% 由调用方通过 Signal.is_st 标注; 成交量参与率上限。
- ⚠️ 未建模(详见 docs/backtest_known_limits.md):
  1. 盘中触及涨停但未封死 —— 实际可排队成交, 本引擎按可成交处理(乐观);
  2. 涨停打开/回封的动态判定 —— 需要分钟级数据, 日K粒度无法区分;
  3. 板块属性自动判定(主板 10%/创业板 20%/科创板 20%/北交所 30%) ——
     目前仅靠 is_st 布尔区分 5%/10%, 创业板/科创板/北交所需调用方自行核对前收。
- 以上限制会使回测收益**系统性偏乐观**(尤其涨停打板策略), 使用回测结论时须知悉。

另提供 horizon_return():复刻 strategy_engine.evaluate_strategy_outcomes 口径,用于交叉验证。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Literal

from src.core.backtest import metrics as M
from src.core.backtest.cost_model import CostModel
from src.core.backtest.data_adapter import PriceBar, first_index_after
from src.core.limit_rules import limit_down_price, limit_up_price
from src.core.trading_calendar import TradingCalendarError, add_trading_days, is_trading_day, next_trading_day, prev_trading_day

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Signal:
    """一条待回测信号(对齐 StrategySignalRun 的可执行字段)。"""

    symbol: str
    market: str
    signal_date: str                  # YYYY-MM-DD(信号产生日)
    entry_price: float | None = None  # None = 用下一交易日开盘价
    stop_loss: float | None = None
    target_price: float | None = None
    holding_days: int = 10            # 最大持有交易日(event 模式)
    is_st: bool = False               # B0.2: ST 涨跌停幅度 5%(代码无法判, 由调用方标注)


@dataclass
class BTTrade:
    symbol: str
    market: str
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    quantity: int
    pnl: float
    pnl_pct: float
    fees: float
    exit_reason: str  # stop_loss | target | expire | eod
    holding_bars: int


@dataclass(frozen=True)
class BTOpenPosition:
    symbol: str
    market: str
    entry_date: str
    entry_price: float
    quantity: int
    mark_date: str | None  # None = 未观测到有效收盘，净值暂按入场价计
    mark_price: float
    buy_cash_delta: float
    unrealized_pnl: float
    # history_end: bars end before the last expected market session, not proof of a vendor gap.
    exit_blocked_reason: Literal["t_plus_one", "untradeable", "sample_end", "history_end"]


@dataclass
class BacktestResult:
    trades: list[BTTrade]
    equity_curve: list[float]
    equity_dates: list[str]
    metrics: dict
    initial_capital: float
    skipped: int = 0
    skipped_cash: int = 0   # B2.1: 因现金不足被跳过
    skipped_slots: int = 0  # B2.1: 因并发持仓上限被跳过
    open_positions: list[BTOpenPosition] = field(default_factory=list)


PositionSizer = Callable[[float], int]  # price -> qty


def simulate_exit(
    bars: list[PriceBar],
    entry_idx: int,
    *,
    stop: float | None,
    target: float | None,
    max_hold: int,
    sellable: Callable[[int], bool] | None = None,
    close_at_sample_end: bool = True,
) -> tuple[float, str, str, int] | None:
    """逐日止损/止盈/到期撮合，供单笔与组合引擎共用。

    返回 (exit_price, exit_date, exit_reason, holding_bars)。
    - T+1: 入场日当天不检查(从 entry_idx+1 起);
    - 跳空: 开盘已越过止损/止盈按开盘价成交;
    - 一字跌停(sellable(j)=False)时止损/到期顺延到下一可成交日;
    - 仅在启用样本末日退出、且最后一个有效推进的交易日可卖时做 eod 撮合。
    """
    is_sellable = sellable or (lambda _j: True)
    last_day = bars[entry_idx].date
    held = 0
    last_sellable = None
    for j in range(entry_idx + 1, len(bars)):
        bar = bars[j]
        if bar.date <= last_day:
            continue
        last_day = bar.date
        held += 1
        last_sellable = None
        if not is_sellable(j):
            continue
        last_sellable = bar
        if stop and stop > 0:
            if bar.open <= stop:  # 跳空跌破
                return bar.open, bar.date, "stop_loss", held
            elif bar.low <= stop:
                return stop, bar.date, "stop_loss", held
        if target and target > 0:
            if bar.open >= target:  # 跳空冲高
                return bar.open, bar.date, "target", held
            if bar.high >= target:
                return target, bar.date, "target", held
        if held >= max_hold:
            return bar.close, bar.date, "expire", held
    if close_at_sample_end and last_sellable is not None:
        return last_sellable.close, last_sellable.date, "eod", held
    return None


def fixed_cash_sizer(cash_per_trade: float, lot: int = 100) -> PositionSizer:
    """每笔固定名义资金,买入 lot 的整数倍。"""

    def _size(price: float) -> int:
        if price <= 0:
            return 0
        lots = int((cash_per_trade / price) // lot)
        return max(0, lots * lot)

    return _size


def _parse_day(s):
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def positive_trade_volume(value: float) -> float:
    try:
        volume = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return volume if math.isfinite(volume) and volume > 0 else 0.0


def valid_trade_price(value: float | None) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def expected_trading_boundary(market: str, day: str | None, *, right: bool = False) -> str | None:
    """First/last A-share session inside a calendar boundary; unverifiable stays unknown."""
    if day is None or market.upper() not in {'CN', 'SH', 'SZ', 'BJ'}:
        return None
    try:
        if is_trading_day(day):
            return day
        return (prev_trading_day(day) if right else next_trading_day(day)).isoformat()
    except (TradingCalendarError, ValueError, OverflowError):
        return None


def _prev_close(bars: list[PriceBar], idx: int) -> float | None:
    """Limit prices need a usable prior close; NaN/None would crash the rounding helper."""
    if idx <= 0 or not valid_trade_price(bars[idx - 1].close):
        return None
    return bars[idx - 1].close


class Backtester:
    def __init__(
        self,
        cost_model: CostModel | None = None,
        initial_capital: float = 1_000_000.0,
        cash_per_trade: float = 100_000.0,
        lot: int = 100,
        sizer: PositionSizer | None = None,
        participation_rate: float = 0.05,
        *,
        sample_end: str | None = None,
        sample_start: str | None = None,
    ) -> None:
        self.cost = cost_model or CostModel()
        self.initial_capital = float(initial_capital)
        self.lot = max(1, int(lot))
        self.participation_rate = max(0.0, float(participation_rate))
        self.sample_end = sample_end
        self.sample_start = sample_start
        self.sizer = sizer or fixed_cash_sizer(cash_per_trade, lot)

    def _buyable(self, bars: list[PriceBar], idx: int, signal: Signal) -> bool:
        """一字涨停(全天封死)时买不进 → False; 无法判定(非 A 股代码/缺昨收) → True。"""
        up = limit_up_price(signal.symbol, _prev_close(bars, idx), signal.is_st)
        if up is None:
            return True
        bar = bars[idx]
        return not (bar.open >= up - 1e-9 and bar.high <= up + 1e-9)

    def _sellable(self, bars: list[PriceBar], idx: int, signal: Signal) -> bool:
        """无有效成交量或一字跌停不可卖；缺限价时保留日线模型原假设。"""
        bar = bars[idx]
        if not positive_trade_volume(bar.volume):
            return False
        if not all(valid_trade_price(p) for p in (bar.open, bar.high, bar.low, bar.close)):
            return False
        dn = limit_down_price(signal.symbol, _prev_close(bars, idx), signal.is_st)
        if dn is None:
            return True
        return not (bar.open <= dn + 1e-9 and bar.low >= dn - 1e-9)

    def _cap_qty(self, qty: int, volume: float) -> int:
        """按当日成交量参与率裁剪(向下取整到一手); 无成交量数据 → 0(保守不可成交)。"""
        vol = positive_trade_volume(volume)
        if vol <= 0 or qty <= 0:
            return 0
        cap = int(vol * self.participation_rate)
        cap = (cap // self.lot) * self.lot
        return int(min(int(qty), cap))

    def run_single(self, signal: Signal, bars: list[PriceBar]) -> BTTrade | BTOpenPosition | None:
        """单信号回测:下一交易日开盘入场,逐日止损/止盈/到期平仓。

        B0.2(2026-09-09): 涨跌停不可成交 —— 一字涨停买不进(顺延到下一可成交日,
        全程封板则该信号作废); 一字跌停卖不出(止损/到期顺延); 成交量参与率上限
        (单笔 ≤ 当日成交量 × participation_rate, 不足一手则顺延/作废)。
        买入后未能卖出返回 BTOpenPosition；没有买入则返回 None。
        """
        if not bars:
            return None
        if self.sample_end is not None:
            bars = [b for b in bars if b.date <= self.sample_end]
            if not bars:
                return None
        ei = first_index_after(bars, signal.signal_date)
        if ei is None or ei >= len(bars):
            return None

        entry_idx = None
        entry_price = 0.0
        qty = 0
        while ei < len(bars):
            bar = bars[ei]
            px = bar.open if signal.entry_price is None else float(signal.entry_price)
            if px > 0 and self._buyable(bars, ei, signal):
                q = self._cap_qty(self.sizer(px), bar.volume)
                if q > 0:
                    entry_idx, entry_price, qty = ei, px, q
                    break
            ei += 1
        if entry_idx is None:
            return None

        stop = signal.stop_loss
        target = signal.target_price
        max_hold = max(1, int(signal.holding_days or 10))

        exit_fill = simulate_exit(
            bars,
            entry_idx,
            stop=stop,
            target=target,
            max_hold=max_hold,
            sellable=lambda j: self._sellable(bars, j, signal),
            close_at_sample_end=self.sample_end is None,
        )
        return self._position_result(signal, bars, entry_idx, entry_price, qty, exit_fill)

    def _position_result(
        self, signal: Signal, bars: list[PriceBar], entry_idx: int, entry_price: float, qty: int,
        exit_fill: tuple[float, str, str, int] | None,
    ) -> BTTrade | BTOpenPosition:
        if exit_fill is None:
            buy = self.cost.fill("buy", entry_price, qty)
            entry_day = bars[entry_idx].date
            latest_day = max(b.date for b in bars[entry_idx:])
            # Only label a reason the bars actually support: an explicit window leaves
            # the position open by choice, not because provider history was missing.
            if latest_day == entry_day:
                reason = "t_plus_one"
            else:
                last_idx = next(i for i in range(entry_idx + 1, len(bars)) if bars[i].date == latest_day)
                if self.sample_end is not None and self._sellable(bars, last_idx, signal):
                    expected_end = expected_trading_boundary(signal.market, self.sample_end, right=True)
                    reason = "history_end" if expected_end and latest_day < expected_end else "sample_end"
                else:
                    reason = "untradeable"
            mark = max(
                (b for b in reversed(bars[entry_idx:])
                 if b.date >= entry_day and valid_trade_price(b.close)),
                key=lambda b: b.date, default=None,
            )
            mark_price = mark.close if mark is not None else entry_price
            return BTOpenPosition(
                symbol=signal.symbol, market=signal.market, entry_date=entry_day,
                entry_price=entry_price, quantity=qty, mark_date=mark.date if mark else None, mark_price=mark_price,
                buy_cash_delta=buy.cash_delta, unrealized_pnl=round(buy.cash_delta + qty * mark_price, 4),
                exit_blocked_reason=reason,
            )
        exit_price, exit_date, exit_reason, held = exit_fill
        rt = self.cost.round_trip_pnl(entry_price, exit_price, qty)
        return BTTrade(
            symbol=signal.symbol,
            market=signal.market,
            entry_date=bars[entry_idx].date,
            entry_price=entry_price,
            exit_date=exit_date,
            exit_price=exit_price,
            quantity=qty,
            pnl=rt["pnl"],
            pnl_pct=rt["pnl_pct"],
            fees=rt["total_cost"],
            exit_reason=exit_reason,
            holding_bars=held,
        )

    @staticmethod
    def _bar_key(trade: BTTrade | BTOpenPosition, bars_by_symbol: dict):
        """定位该笔交易在 bars_by_symbol 中的键((symbol, market) 优先, 退化为 symbol)。"""
        key = (trade.symbol, trade.market)
        if key in bars_by_symbol:
            return key
        if trade.symbol in bars_by_symbol:
            return trade.symbol
        return None

    def _daily_equity_curve(
        self, trades: list[BTTrade], bars_by_symbol: dict, open_positions: list[BTOpenPosition],
    ) -> tuple[list[float], list[str]]:
        """逐交易日净值曲线 = 现金 + 持仓当日收盘市值(含浮动盈亏)。

        B0.1(2026-09-09): 原实现按平仓笔累积已实现盈亏, 与 metrics.py 的
        "逐交易日净值"契约不符, 导致年化/夏普按笔年化、最大回撤忽略持仓期浮亏。
        """
        positions = [*trades, *open_positions]
        if not positions:
            return [self.initial_capital], [""]

        start = min(t.entry_date for t in positions)
        if self.sample_start is not None:
            start = min(start, self.sample_start)
        # Stop at the last actual exit or open-position mark; unrelated symbols
        # cannot extend the measured holding period beyond the evidence.
        end = max([t.exit_date for t in trades] + [p.mark_date or p.entry_date for p in open_positions])
        all_dates = sorted(
            {
                b.date
                for bars in bars_by_symbol.values()
                for b in bars
                if start <= b.date <= end
            }
        )
        if not all_dates:
            return [self.initial_capital], [""]

        close_by_key: dict = {}
        for key, bars in bars_by_symbol.items():
            close_by_key[key] = {b.date: b.close for b in bars if valid_trade_price(b.close)}

        entries: dict[str, list[tuple[int, BTTrade | BTOpenPosition]]] = {}
        exits: dict[str, list[tuple[int, BTTrade]]] = {}
        for idx, t in enumerate(positions):
            entries.setdefault(t.entry_date, []).append((idx, t))
            if isinstance(t, BTTrade):
                exits.setdefault(t.exit_date, []).append((idx, t))

        cash = self.initial_capital
        open_pos: list[dict] = []
        curve: list[float] = [self.initial_capital]
        dates: list[str] = [""]

        for d in all_dates:
            # 先出场(回款)再入场(占款), 同日不重复计
            for idx, t in exits.get(d, []):
                sell = self.cost.fill("sell", t.exit_price, t.quantity)
                cash += sell.cash_delta
                open_pos = [p for p in open_pos if p["tid"] != idx]
            for idx, t in entries.get(d, []):
                cash += (t.buy_cash_delta if isinstance(t, BTOpenPosition)
                         else self.cost.fill("buy", t.entry_price, t.quantity).cash_delta)
                open_pos.append(
                    {
                        "tid": idx,
                        "key": self._bar_key(t, bars_by_symbol),
                        "qty": t.quantity,
                        "last_close": t.entry_price,
                    }
                )

            mv = 0.0
            for p in open_pos:
                p["last_close"] = close_by_key.get(p["key"], {}).get(d, p["last_close"])
                mv += p["qty"] * p["last_close"]
            curve.append(round(cash + mv, 4))
            dates.append(d)

        return curve, dates

    def run(
        self, signals: list[Signal], bars_by_symbol: dict
    ) -> BacktestResult:
        """批量回测, 聚合逐日净值曲线与绩效指标。

        bars_by_symbol: 键可为 (symbol, market) 或 symbol。
        """
        trades: list[BTTrade] = []
        open_positions: list[BTOpenPosition] = []
        skipped = 0
        for sig in signals:
            bars = bars_by_symbol.get((sig.symbol, sig.market)) or bars_by_symbol.get(sig.symbol)
            if not bars:
                skipped += 1
                continue
            t = self.run_single(sig, bars)
            if t is None:
                skipped += 1
                continue
            if isinstance(t, BTOpenPosition):
                open_positions.append(t)
            else:
                trades.append(t)

        trades_sorted = sorted(trades, key=lambda t: (t.exit_date, t.entry_date))
        curve, dates = self._daily_equity_curve(trades_sorted, bars_by_symbol, open_positions)
        M.validate_equity_curve(curve, dates)

        pnls = [t.pnl for t in trades]
        metrics = M.summarize(curve, pnls)
        metrics.update(
            open_positions=len(open_positions),
            unrealized_pnl=round(sum(p.unrealized_pnl for p in open_positions), 4),
        )
        return BacktestResult(
            trades=trades,
            equity_curve=curve,
            equity_dates=dates,
            metrics=metrics,
            initial_capital=self.initial_capital,
            skipped=skipped,
            open_positions=open_positions,
        )


def horizon_return(signal: Signal, bars: list[PriceBar], horizon_days: int) -> float | None:
    """复刻 strategy_engine.evaluate_strategy_outcomes 口径,用于交叉验证。

    B0.3(2026-09-09): target_day = signal_date 起第 horizon_days 个**交易日**
    (原实现用自然日 → "5 日"实为约 3 个交易日); outcome = target_day **当日**收盘,
    缺失即 None(不再回退更早收盘)。
    """
    snap = _parse_day(signal.signal_date)
    base = signal.entry_price
    if snap is None or not bars or not base or base <= 0:
        return None
    target = add_trading_days(snap, int(horizon_days)).isoformat()
    for b in bars:
        if b.date == target:
            return (b.close - base) / base * 100.0
    return None
