"""参数扫描 + 滚动(walk-forward)验证(B2.2/B2.3)。

在 `service.run_backtest`(单次回测) 之上提供两件研究基础设施:
- `sweep`: 参数网格逐组合回测, 按指标排序(默认夏普), 输出可直接落实验日志的结果表;
- `walk_forward`: 训练窗内选参、测试窗只读评估, 逐窗滚动, 汇总样本外表现。

诚实约束: 训练窗选参与测试窗评估**严格分离**; 汇总只报测试窗指标, 不拿训练窗数字充数。
`trades`/`oos_trades` 只数已平仓；无法在末日成交的仓位留作未平仓并单独披露。
"""

from __future__ import annotations

import itertools
from bisect import bisect_left, bisect_right
from dataclasses import dataclass

from src.core.backtest.data_adapter import PriceBar
from src.core.backtest.engine import Backtester, Signal

DEFAULT_FASTS = (3, 5, 8, 10)
DEFAULT_SLOWS = (20, 30, 40, 60)


def golden_cross_signals(
    symbol: str,
    market: str,
    bars: list[PriceBar],
    *,
    fast: int = 5,
    slow: int = 20,
    holding_days: int = 10,
    stop_pct: float = 0.08,
    target_pct: float = 0.15,
) -> list[Signal]:
    """MA(fast) 上穿 MA(slow) → 信号(仅用截至当日收盘, 无未来函数)。"""
    if fast <= 0 or slow <= 0 or fast >= slow:
        return []
    closes = [b.close for b in bars]
    out: list[Signal] = []
    for i in range(slow, len(bars)):
        prev_fast = sum(closes[i - fast:i]) / fast
        prev_slow = sum(closes[i - slow:i]) / slow
        cur_fast = sum(closes[i - fast + 1:i + 1]) / fast
        cur_slow = sum(closes[i - slow + 1:i + 1]) / slow
        if prev_fast <= prev_slow and cur_fast > cur_slow:
            c = closes[i]
            out.append(
                Signal(
                    symbol=symbol,
                    market=market,
                    signal_date=bars[i].date,
                    stop_loss=round(c * (1 - stop_pct), 4) if stop_pct > 0 else None,
                    target_price=round(c * (1 + target_pct), 4) if target_pct > 0 else None,
                    holding_days=holding_days,
                )
            )
    return out


@dataclass
class _Run:
    params: dict
    result: object  # BacktestResult


def run_once(
    bars_by_symbol: dict,
    *,
    fast: int = 5,
    slow: int = 20,
    holding_days: int = 10,
    stop_pct: float = 0.08,
    target_pct: float = 0.15,
    initial_capital: float = 1_000_000.0,
    cash_per_trade: float = 100_000.0,
    participation_rate: float = 0.05,
    sample_start: str | None = None,
    sample_end: str | None = None,
):
    """用给定参数跑一次金叉策略, 返回 BacktestResult。

    传 `sample_end` 时末日不强平, 窗口末端无法卖出的仓位留作未平仓;
    研究切片必须传它, 否则会得到一个窗口外不可能发生的末日成交。
    """
    signals: list[Signal] = []
    for key, bars in bars_by_symbol.items():
        symbol, market = key if isinstance(key, tuple) else (str(key), "CN")
        signals.extend(
            golden_cross_signals(
                symbol, market, bars,
                fast=fast, slow=slow, holding_days=holding_days,
                stop_pct=stop_pct, target_pct=target_pct,
            )
        )
    bt = Backtester(
        initial_capital=initial_capital,
        cash_per_trade=cash_per_trade,
        participation_rate=participation_rate,
        sample_start=sample_start,
        sample_end=sample_end,
    )
    if sample_start is not None:
        signals = [signal for signal in signals if signal.signal_date >= sample_start]
    return bt.run(signals, bars_by_symbol)


def _score(metrics: dict, by: str) -> float:
    v = metrics.get(by)
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("-inf")


def sweep(
    bars_by_symbol: dict,
    *,
    fasts: tuple[int, ...] = DEFAULT_FASTS,
    slows: tuple[int, ...] = DEFAULT_SLOWS,
    holdings: tuple[int, ...] = (5, 10, 20),
    by: str = "sharpe",
    top_n: int = 10,
    **run_kwargs,
) -> list[dict]:
    """参数网格扫描, 按 `by` 指标倒序返回前 `top_n` 组。"""
    rows: list[dict] = []
    for fast, slow, hold in itertools.product(fasts, slows, holdings):
        if fast >= slow:
            continue
        res = run_once(bars_by_symbol, fast=fast, slow=slow, holding_days=hold, **run_kwargs)
        rows.append(
            {
                "params": {"fast": fast, "slow": slow, "holding_days": hold},
                "metrics": res.metrics,
                "trades": len(res.trades),
                "open_positions": len(res.open_positions),
                "score": _score(res.metrics, by),
            }
        )
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[: max(1, int(top_n))]


def walk_forward(
    bars_by_symbol: dict,
    *,
    fasts: tuple[int, ...] = DEFAULT_FASTS,
    slows: tuple[int, ...] = DEFAULT_SLOWS,
    holdings: tuple[int, ...] = (5, 10),
    train_bars: int = 60,
    test_bars: int = 20,
    step: int | None = None,
    by: str = "sharpe",
    **run_kwargs,
) -> dict:
    """滚动验证: 训练窗选参(含 warmup), 测试窗只读评估。

    返回 {windows: [{train_end, test_start, test_end, best_params, is_score, oos_metrics}],
          oos_summary: {windows, avg_sharpe, avg_return, total_trades, total_open_positions}}
    下标对应全标的观察日期的有序并集，不是单一标的 K 线的下标。训练只看切点前，
    测试可用训练尾部预热均线，但不交易或统计测试窗之前的信号和净值。
    """
    if not bars_by_symbol:
        return {"windows": [], "oos_summary": {"windows": 0, "avg_sharpe": None,
                                               "avg_return": None, "total_trades": 0,
                                               "total_open_positions": 0}}
    step = test_bars if step is None else step
    if min(train_bars, test_bars, step) <= 0:
        raise ValueError("train_bars, test_bars and step must be positive")
    series = {k: sorted(v, key=lambda b: b.date) for k, v in bars_by_symbol.items() if v}
    dates_by_symbol = {k: [b.date for b in v] for k, v in series.items()}
    days = sorted({d for dates in dates_by_symbol.values() for d in dates})
    windows: list[dict] = []
    strategy_kwargs = {k: v for k, v in run_kwargs.items() if k not in ('sample_start', 'sample_end')}
    pos = train_bars
    while pos + test_bars <= len(days):
        train_start, train_end = days[0], days[pos - 1]
        test_start, test_end = days[pos], days[pos + test_bars - 1]
        stops = {k: bisect_left(dates, test_start) for k, dates in dates_by_symbol.items()}
        train = {k: bars[:stops[k]] for k, bars in series.items()}
        ranked = sweep(train, fasts=fasts, slows=slows, holdings=holdings, by=by,
                       top_n=1, sample_start=train_start, sample_end=train_end, **strategy_kwargs)
        if ranked:
            best = ranked[0]
            warmup = best["params"]["slow"]
            test = {k: bars[max(0, stops[k] - warmup):bisect_right(dates_by_symbol[k], test_end)]
                    for k, bars in series.items()}
            oos = run_once(test, sample_start=test_start, sample_end=test_end,
                           **best["params"], **strategy_kwargs)
            windows.append(
                {
                    "train_end": pos,
                    "test_start": pos,
                    "test_end": pos + test_bars,
                    "train_end_date": train_end,
                    "test_start_date": test_start,
                    "test_end_date": test_end,
                    "best_params": best["params"],
                    "is_score": best["score"],
                    "oos_metrics": oos.metrics,
                    "oos_trades": len(oos.trades),
                    "oos_open_positions": len(oos.open_positions),
                }
            )
        pos += step

    sharpes = [w["oos_metrics"].get("sharpe") for w in windows
               if isinstance(w["oos_metrics"].get("sharpe"), (int, float))]
    rets = [w["oos_metrics"].get("total_return") for w in windows
            if isinstance(w["oos_metrics"].get("total_return"), (int, float))]
    return {
        "windows": windows,
        "oos_summary": {
            "windows": len(windows),
            "avg_sharpe": round(sum(sharpes) / len(sharpes), 4) if sharpes else None,
            "avg_return": round(sum(rets) / len(rets), 6) if rets else None,
            "total_trades": sum(w["oos_trades"] for w in windows),
            "total_open_positions": sum(w["oos_open_positions"] for w in windows),
        },
    }
