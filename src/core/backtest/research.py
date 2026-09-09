"""参数扫描 + 滚动(walk-forward)验证(B2.2/B2.3)。

在 `service.run_backtest`(单次回测) 之上提供两件研究基础设施:
- `sweep`: 参数网格逐组合回测, 按指标排序(默认夏普), 输出可直接落实验日志的结果表;
- `walk_forward`: 训练窗内选参、测试窗只读评估, 逐窗滚动, 汇总样本外表现。

诚实约束: 训练窗选参与测试窗评估**严格分离**; 汇总只报测试窗指标, 不拿训练窗数字充数。
"""

from __future__ import annotations

import itertools
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
):
    """用给定参数跑一次金叉策略, 返回 BacktestResult。"""
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
    )
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
                "score": _score(res.metrics, by),
            }
        )
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows[: max(1, int(top_n))]


def _slice(bars_by_symbol: dict, start: int, end: int | None) -> dict:
    return {k: v[start:end] for k, v in bars_by_symbol.items()}


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
          oos_summary: {windows, avg_sharpe, avg_return, total_trades}}
    """
    if not bars_by_symbol:
        return {"windows": [], "oos_summary": {"windows": 0, "avg_sharpe": None,
                                               "avg_return": None, "total_trades": 0}}
    length = min(len(v) for v in bars_by_symbol.values())
    step = step or test_bars
    windows: list[dict] = []
    pos = train_bars
    while pos + test_bars <= length:
        train = _slice(bars_by_symbol, 0, pos)
        test = _slice(bars_by_symbol, pos, pos + test_bars)
        ranked = sweep(train, fasts=fasts, slows=slows, holdings=holdings, by=by,
                       top_n=1, **run_kwargs)
        if ranked:
            best = ranked[0]
            oos = run_once(test, **best["params"], **run_kwargs)
            windows.append(
                {
                    "train_end": pos,
                    "test_start": pos,
                    "test_end": pos + test_bars,
                    "best_params": best["params"],
                    "is_score": best["score"],
                    "oos_metrics": oos.metrics,
                    "oos_trades": len(oos.trades),
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
        },
    }
