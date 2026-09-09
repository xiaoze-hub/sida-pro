"""组合级回测撮合(B2.1)。

与 `Backtester.run`(逐信号独立回测、各笔独占固定资金)的区别: 本引擎维护**共享现金账户
与持仓槽位**, 在现金不足 / 并发上限 / 成交量容量约束下决定哪些信号真正成交, 并按日
mark-to-market 出净值曲线。

约束(全部可配):
- 现金: 建仓支出(含费) ≤ 当前可用现金(不足则按手数缩量, 缩不到一手则跳过);
- 并发: 同时持仓数 ≤ `max_positions`(同日出场先释放槽位);
- 仓位: 单笔目标名义 = `initial_capital × position_pct`;
- 容量: 单笔 ≤ 当日成交量 × `participation_rate`(与 B0.2 同口径);
- 涨跌停: 一字涨停买不进 / 一字跌停卖不出(复用内核规则与 `simulate_exit`)。
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.backtest import metrics as M
from src.core.backtest.cost_model import CostModel
from src.core.backtest.data_adapter import first_index_after
from src.core.backtest.engine import (
    BacktestResult,
    Backtester,
    BTTrade,
    Signal,
    simulate_exit,
)


@dataclass
class PortfolioConfig:
    initial_capital: float = 1_000_000.0
    position_pct: float = 0.10   # 单笔目标名义占期初资金比例
    max_positions: int = 10      # 并发持仓上限
    lot: int = 100
    participation_rate: float = 0.05


class PortfolioBacktester:
    """共享资金账户的组合级回测。"""

    def __init__(self, config: PortfolioConfig | None = None, cost_model: CostModel | None = None):
        self.cfg = config or PortfolioConfig()
        self.cost = cost_model or CostModel()
        # 复用内核的涨跌停/容量/单笔撮合规则(不重复实现)
        self._single = Backtester(
            cost_model=self.cost,
            initial_capital=self.cfg.initial_capital,
            cash_per_trade=self.cfg.initial_capital * self.cfg.position_pct,
            lot=self.cfg.lot,
            participation_rate=self.cfg.participation_rate,
        )

    def _candidate(self, sig: Signal, bars: list):
        """为信号找到可成交的入场 (idx, price, 容量内手数); 无则 None。"""
        ei = first_index_after(bars, sig.signal_date)
        while ei is not None and ei < len(bars):
            bar = bars[ei]
            px = bar.open if sig.entry_price is None else float(sig.entry_price)
            if px > 0 and self._single._buyable(bars, ei, sig):
                qty = self._single._cap_qty(self._single.sizer(px), bar.volume)
                if qty > 0:
                    return ei, px, qty
            ei += 1
        return None

    def run(self, signals: list[Signal], bars_by_symbol: dict) -> BacktestResult:
        """按入场日时序撮合: 先释放到期/出场, 再受现金与槽位约束建仓。"""
        planned = []
        for sig in signals:
            bars = bars_by_symbol.get((sig.symbol, sig.market)) or bars_by_symbol.get(sig.symbol)
            if not bars:
                continue
            cand = self._candidate(sig, bars)
            if cand is None:
                continue
            ei, px, qty = cand
            exit_price, exit_date, exit_reason, held = simulate_exit(
                bars,
                ei,
                stop=sig.stop_loss,
                target=sig.target_price,
                max_hold=max(1, int(sig.holding_days or 10)),
                sellable=lambda j: self._single._sellable(bars, j, sig),
            )
            planned.append(
                {
                    "sig": sig,
                    "bars": bars,
                    "entry_idx": ei,
                    "entry_date": bars[ei].date,
                    "entry_price": px,
                    "want_qty": qty,
                    "exit_price": exit_price,
                    "exit_date": exit_date,
                    "exit_reason": exit_reason,
                    "held": held,
                }
            )

        planned.sort(key=lambda x: (x["entry_date"], x["sig"].symbol))

        cash = float(self.cfg.initial_capital)
        open_pos: list[dict] = []   # 仅需 exit_date 以释放槽位
        trades: list[BTTrade] = []
        skipped_cash = skipped_slots = 0

        for p in planned:
            d = p["entry_date"]
            # 1) 先释放当日及之前出场的持仓(回款 + 腾槽位)
            still_open = []
            for op in open_pos:
                if op["exit_date"] <= d:
                    # 建仓支出已在入场日扣减; 出场仅回款(含卖出规费)
                    cash += self.cost.fill("sell", op["exit_price"], op["qty"]).cash_delta
                else:
                    still_open.append(op)
            open_pos = still_open

            # 2) 并发上限
            if len(open_pos) >= max(1, int(self.cfg.max_positions)):
                skipped_slots += 1
                continue

            # 3) 现金约束: 目标手数买不起则按手数缩量
            qty = int(p["want_qty"])
            lot = max(1, int(self.cfg.lot))
            while qty >= lot and -self.cost.fill("buy", p["entry_price"], qty).cash_delta > cash:
                qty -= lot
            if qty < lot:
                skipped_cash += 1
                continue

            buy = self.cost.fill("buy", p["entry_price"], qty)
            cash += buy.cash_delta  # 负数
            open_pos.append(
                {
                    "entry_price": p["entry_price"],
                    "exit_price": p["exit_price"],
                    "exit_date": p["exit_date"],
                    "qty": qty,
                }
            )
            rt = self.cost.round_trip_pnl(p["entry_price"], p["exit_price"], qty)
            trades.append(
                BTTrade(
                    symbol=p["sig"].symbol,
                    market=p["sig"].market,
                    entry_date=p["entry_date"],
                    entry_price=round(p["entry_price"], 4),
                    exit_date=p["exit_date"],
                    exit_price=round(p["exit_price"], 4),
                    quantity=qty,
                    pnl=rt["pnl"],
                    pnl_pct=rt["pnl_pct"],
                    fees=rt["total_cost"],
                    exit_reason=p["exit_reason"],
                    holding_bars=p["held"],
                )
            )

        trades_sorted = sorted(trades, key=lambda t: (t.exit_date, t.entry_date))
        curve, dates = self._single._daily_equity_curve(trades_sorted, bars_by_symbol)
        M.validate_equity_curve(curve, dates)
        return BacktestResult(
            trades=trades,
            equity_curve=curve,
            equity_dates=dates,
            metrics=M.summarize(curve, [t.pnl for t in trades]),
            initial_capital=self.cfg.initial_capital,
            skipped=skipped_cash + skipped_slots,
            skipped_cash=skipped_cash,
            skipped_slots=skipped_slots,
        )
