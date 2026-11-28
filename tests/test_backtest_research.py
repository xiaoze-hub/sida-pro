"""B2.2/B2.3: 参数扫描 + walk-forward(训练/测试严格分离)。"""

from __future__ import annotations

from src.core.backtest.data_adapter import PriceBar
from src.core.backtest.research import golden_cross_signals, run_once, sweep, walk_forward


def _trend_bars(n: int = 160, start: float = 10.0, wave: float = 0.004) -> list[PriceBar]:
    """带波段的趋势序列, 保证金叉会反复出现。"""
    out = []
    price = start
    for i in range(n):
        price *= 1 + wave * (1 if (i // 12) % 2 == 0 else -0.6)
        out.append(PriceBar(date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                            open=price, high=price * 1.01, low=price * 0.99,
                            close=price, volume=1e6))
    return out


def _universe() -> dict:
    return {("600000", "CN"): _trend_bars(), ("600001", "CN"): _trend_bars(wave=0.005)}


def test_golden_cross_signals_parameterized():
    bars = _trend_bars()
    fast_sig = golden_cross_signals("600000", "CN", bars, fast=3, slow=10)
    slow_sig = golden_cross_signals("600000", "CN", bars, fast=10, slow=60)
    assert fast_sig, "短周期应产生信号"
    assert len(fast_sig) >= len(slow_sig), "短周期信号不应少于长周期"
    assert golden_cross_signals("600000", "CN", bars, fast=20, slow=10) == []  # 非法参数


def test_run_once_and_sweep_sorted():
    uni = _universe()
    res = run_once(uni, fast=5, slow=20, holding_days=10)
    assert "sharpe" in res.metrics and "max_drawdown" in res.metrics

    rows = sweep(uni, fasts=(3, 5), slows=(20, 40), holdings=(5,), top_n=4)
    assert rows and all("params" in r and "metrics" in r for r in rows)
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True), "应按指标倒序"
    assert all(r["params"]["fast"] < r["params"]["slow"] for r in rows)


def test_walk_forward_separates_train_and_test():
    uni = _universe()
    out = walk_forward(uni, fasts=(3, 5), slows=(20, 40), holdings=(5,),
                       train_bars=60, test_bars=20, step=20)
    assert out["windows"], "至少一个滚动窗口"
    for w in out["windows"]:
        assert w["best_params"]["fast"] < w["best_params"]["slow"]
        assert "sharpe" in w["oos_metrics"]
        assert w["test_end"] > w["test_start"]
    summ = out["oos_summary"]
    assert summ["windows"] == len(out["windows"])
    assert summ["total_trades"] == sum(w["oos_trades"] for w in out["windows"])
    assert summ["total_open_positions"] == sum(w["oos_open_positions"] for w in out["windows"])
    # 汇总只来自测试窗(样本外), 不混入训练窗指标
    assert summ["avg_sharpe"] is None or isinstance(summ["avg_sharpe"], float)


def test_walk_forward_does_not_liquidate_on_the_window_end_bar():
    """A slice's final bar is not a taker exit: positions stay open and are disclosed."""
    from src.core.backtest.data_adapter import PriceBar
    from src.core.backtest.research import run_once

    days = [f"2026-{month:02d}-{day:02d}" for month, day in
            ((10, 1), (10, 2), (10, 5), (10, 6), (10, 7), (10, 8), (10, 9))]
    bars = [PriceBar(d, 10, 10.2, 9.8, 10 + i * 0.01, 100_000) for i, d in enumerate(days)]
    without_end = run_once({'X': bars}, fast=3, slow=5, holding_days=10)
    with_end = run_once({'X': bars}, fast=3, slow=5, holding_days=10, sample_end=days[-1])
    assert with_end.sample_end == days[-1]
    assert len(with_end.open_positions) >= len(without_end.open_positions)
    assert not any(t.exit_date == days[-1] and t.exit_reason == "eod" for t in with_end.trades)


def test_walk_forward_empty_universe():
    out = walk_forward({})
    assert out["windows"] == [] and out["oos_summary"]["windows"] == 0


def test_walk_forward_owns_both_slice_bounds_even_with_caller_overrides(monkeypatch):
    from src.core.backtest import research

    blocks = []
    original = research.run_once

    def record(bars, **kwargs):
        result = original(bars, **kwargs)
        blocks.append((bars, result))
        return result

    monkeypatch.setattr(research, 'run_once', record)
    output = research.walk_forward({'X': _trend_bars(80)}, fasts=(3,), slows=(20,), holdings=(60,),
                                   train_bars=60, test_bars=20, sample_start=None, sample_end=None)
    assert output['oos_summary']['windows'] == 1
    assert len(blocks) == 2
    for bars, result in blocks:
        assert result.sample_end == bars['X'][-1].date
        assert result.equity_dates[1] == result.sample_start
        assert all(t.exit_reason != 'eod' for t in result.trades)
    assert blocks[0][1].sample_start == blocks[0][0]['X'][0].date
    assert blocks[1][1].sample_start == _trend_bars(80)[60].date
    assert blocks[1][0]['X'][0].date < blocks[1][1].sample_start


def test_walk_forward_short_oos_can_trade_with_training_tail_as_warmup():
    bars = _trend_bars(80)
    out = walk_forward({'X': bars}, fasts=(3,), slows=(20,), holdings=(2,))
    assert out['oos_summary']['windows'] == 1
    assert out['oos_summary']['total_trades'] > 0


def test_walk_forward_uses_one_date_cut_for_staggered_symbols():
    bars = _trend_bars(95)
    out = walk_forward({'X': bars[:80], 'Y': bars[15:]}, fasts=(3,), slows=(20,), holdings=(2,))
    w = out['windows'][0]
    assert w['train_end_date'] == bars[59].date < w['test_start_date'] == bars[60].date
