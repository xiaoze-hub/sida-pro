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
- 涨跌停无法成交约束未建模(TODO:需前收 + 板块判定)。

另提供 horizon_return():复刻 strategy_engine.evaluate_strategy_outcomes 口径,用于交叉验证。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from src.core.backtest import metrics as M
from src.core.backtest.cost_model import CostModel
from src.core.backtest.data_adapter import PriceBar, first_index_after
from src.core.limit_rules import limit_down_price, limit_up_price
from src.core.trading_calendar import add_trading_days

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


PositionSizer = Callable[[float], int]  # price -> qty


def simulate_exit(
    bars: list[PriceBar],
    entry_idx: int,
    *,
    stop: float | None,
    target: float | None,
    max_hold: int,
    sellable: Callable[[int], bool] | None = None,
) -> tuple[float, str, str, int]:
    """逐日止损/止盈/到期撮合(B2.1 抽出, 供单笔与组合引擎共用, 行为零变更)。

    返回 (exit_price, exit_date, exit_reason, holding_bars)。
    - T+1: 入场日当天不检查(从 entry_idx+1 起);
    - 跳空: 开盘已越过止损/止盈按开盘价成交;
    - 一字跌停(sellable(j)=False)时止损/到期顺延到下一可成交日;
    - 全程未触发 → 最后一根收盘(eod)。
    """
    is_sellable = sellable or (lambda _j: True)
    for j in range(entry_idx + 1, len(bars)):
        held = j - entry_idx
        bar = bars[j]
        if stop and stop > 0:
            if bar.open <= stop:  # 跳空跌破
                if is_sellable(j):
                    return bar.open, bar.date, "stop_loss", held
            elif bar.low <= stop:
                if is_sellable(j):
                    return stop, bar.date, "stop_loss", held
        if target and target > 0:
            if bar.open >= target:  # 跳空冲高
                return bar.open, bar.date, "target", held
            if bar.high >= target:
                return target, bar.date, "target", held
        if held >= max_hold:
            if is_sellable(j):
                return bar.close, bar.date, "expire", held
    last = bars[-1]
    return last.close, last.date, "eod", len(bars) - 1 - entry_idx


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


class Backtester:
    def __init__(
        self,
        cost_model: CostModel | None = None,
        initial_capital: float = 1_000_000.0,
        cash_per_trade: float = 100_000.0,
        lot: int = 100,
        sizer: PositionSizer | None = None,
        participation_rate: float = 0.05,
    ) -> None:
        self.cost = cost_model or CostModel()
        self.initial_capital = float(initial_capital)
        self.lot = max(1, int(lot))
        self.participation_rate = max(0.0, float(participation_rate))
        self.sizer = sizer or fixed_cash_sizer(cash_per_trade, lot)

    def _buyable(self, bars: list[PriceBar], idx: int, signal: Signal) -> bool:
        """一字涨停(全天封死)时买不进 → False; 无法判定(非 A 股代码/缺昨收) → True。"""
        up = limit_up_price(
            signal.symbol, bars[idx - 1].close if idx > 0 else None, signal.is_st
        )
        if up is None:
            return True
        bar = bars[idx]
        return not (bar.open >= up - 1e-9 and bar.high <= up + 1e-9)

    def _sellable(self, bars: list[PriceBar], idx: int, signal: Signal) -> bool:
        """一字跌停(全天封死)时卖不出 → False; 无法判定 → True。"""
        dn = limit_down_price(
            signal.symbol, bars[idx - 1].close if idx > 0 else None, signal.is_st
        )
        if dn is None:
            return True
        bar = bars[idx]
        return not (bar.open <= dn + 1e-9 and bar.low >= dn - 1e-9)

    def _cap_qty(self, qty: int, volume: float) -> int:
        """按当日成交量参与率裁剪(向下取整到一手); 无成交量数据 → 0(保守不可成交)。"""
        try:
            vol = float(volume or 0.0)
        except Exception:
            vol = 0.0
        if vol <= 0 or qty <= 0:
            return 0
        cap = int(vol * self.participation_rate)
        cap = (cap // self.lot) * self.lot
        return int(min(int(qty), cap))

    def run_single(self, signal: Signal, bars: list[PriceBar]) -> BTTrade | None:
        """单信号回测:下一交易日开盘入场,逐日止损/止盈/到期平仓。

        B0.2(2026-09-09): 涨跌停不可成交 —— 一字涨停买不进(顺延到下一可成交日,
        全程封板则该信号作废); 一字跌停卖不出(止损/到期顺延); 成交量参与率上限
        (单笔 ≤ 当日成交量 × participation_rate, 不足一手则顺延/作废)。
        """
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

        # B2.1: 出场撮合抽到 simulate_exit(单笔/组合引擎共用), 行为与原实现一致
        exit_price, exit_date, exit_reason, held = simulate_exit(
            bars,
            entry_idx,
            stop=stop,
            target=target,
            max_hold=max_hold,
            sellable=lambda j: self._sellable(bars, j, signal),
        )

        rt = self.cost.round_trip_pnl(entry_price, exit_price, qty)
        return BTTrade(
            symbol=signal.symbol,
            market=signal.market,
            entry_date=bars[entry_idx].date,
            entry_price=round(entry_price, 4),
            exit_date=exit_date,
            exit_price=round(exit_price, 4),
            quantity=qty,
            pnl=rt["pnl"],
            pnl_pct=rt["pnl_pct"],
            fees=rt["total_cost"],
            exit_reason=exit_reason,
            holding_bars=held,
        )

    @staticmethod
    def _bar_key(trade: BTTrade, bars_by_symbol: dict):
        """定位该笔交易在 bars_by_symbol 中的键((symbol, market) 优先, 退化为 symbol)。"""
        key = (trade.symbol, trade.market)
        if key in bars_by_symbol:
            return key
        if trade.symbol in bars_by_symbol:
            return trade.symbol
        return None

    def _daily_equity_curve(
        self, trades: list[BTTrade], bars_by_symbol: dict
    ) -> tuple[list[float], list[str]]:
        """逐交易日净值曲线 = 现金 + 持仓当日收盘市值(含浮动盈亏)。

        B0.1(2026-09-09): 原实现按平仓笔累积已实现盈亏, 与 metrics.py 的
        "逐交易日净值"契约不符, 导致年化/夏普按笔年化、最大回撤忽略持仓期浮亏。
        """
        if not trades:
            return [self.initial_capital], [""]

        start = min(t.entry_date for t in trades)
        end = max(t.exit_date for t in trades)
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
            close_by_key[key] = {b.date: b.close for b in bars}

        entries: dict[str, list[tuple[int, BTTrade]]] = {}
        exits: dict[str, list[tuple[int, BTTrade]]] = {}
        for idx, t in enumerate(trades):
            entries.setdefault(t.entry_date, []).append((idx, t))
            exits.setdefault(t.exit_date, []).append((idx, t))

        cash = self.initial_capital
        open_pos: list[dict] = []
        curve: list[float] = []
        dates: list[str] = []

        for d in all_dates:
            # 先出场(回款)再入场(占款), 同日不重复计
            for idx, t in exits.get(d, []):
                sell = self.cost.fill("sell", t.exit_price, t.quantity)
                cash += sell.cash_delta
                open_pos = [p for p in open_pos if p["tid"] != idx]
            for idx, t in entries.get(d, []):
                buy = self.cost.fill("buy", t.entry_price, t.quantity)
                cash += buy.cash_delta
                open_pos.append(
                    {
                        "tid": idx,
                        "key": self._bar_key(t, bars_by_symbol),
                        "qty": t.quantity,
                        "entry_price": t.entry_price,
                    }
                )

            mv = 0.0
            for p in open_pos:
                px = close_by_key.get(p["key"], {}).get(d)
                if px is None:
                    px = p["entry_price"]  # 当日无行情(停牌等)按成本估值, 保守
                mv += p["qty"] * px
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
            trades.append(t)

        trades_sorted = sorted(trades, key=lambda t: (t.exit_date, t.entry_date))
        curve, dates = self._daily_equity_curve(trades_sorted, bars_by_symbol)
        M.validate_equity_curve(curve, dates)

        pnls = [t.pnl for t in trades]
        return BacktestResult(
            trades=trades,
            equity_curve=curve,
            equity_dates=dates,
            metrics=M.summarize(curve, pnls),
            initial_capital=self.initial_capital,
            skipped=skipped,
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
