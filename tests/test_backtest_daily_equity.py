"""B0.1: 回测净值曲线必须逐日 mark-to-market(含持仓浮动盈亏)。

背景: 原 engine.run 按平仓笔累积已实现盈亏, 与 metrics.py 的
"逐交易日净值序列"契约不符 → 年化/夏普按笔年化、MDD 漏掉持仓期浮亏。
"""

import pytest

from src.core.backtest import metrics as M
from src.core.backtest.data_adapter import PriceBar
from src.core.backtest.engine import Backtester, Signal


def _bar(date, o, h, low, c, v=1e6):
    return PriceBar(date=date, open=o, high=h, low=low, close=c, volume=v)


def test_equity_curve_is_daily_and_spans_all_bars():
    """净值曲线长度 = 回测区间内交易日数, 不是成交笔数。"""
    bars = [_bar(f"2026-01-{d:02d}", 10, 10.1, 9.9, 10) for d in range(1, 11)]
    sig = Signal("X", "CN", "2026-01-01", stop_loss=5, target_price=99, holding_days=10)
    res = Backtester().run([sig], {("X", "CN"): bars})
    assert res.trades, "应有 1 笔成交"
    # 入场 01-02, 末根平仓 → 曲线覆盖 01-02..01-10 共 9 个交易日
    assert len(res.equity_curve) == len(res.equity_dates) == 9
    assert res.equity_dates == [f"2026-01-{d:02d}" for d in range(2, 11)]


def test_equity_marks_floating_loss():
    """持仓期内的浮亏必须体现在最大回撤里(旧实现按笔累积会漏掉)。"""
    closes = [10, 10, 8, 8, 8, 12, 12, 12]  # 入场后跌到 8 再回到 12
    bars = [
        _bar(f"2026-01-{i + 1:02d}", c, c * 1.01, c * 0.99, c)
        for i, c in enumerate(closes)
    ]
    sig = Signal("X", "CN", "2026-01-01", stop_loss=5, target_price=99, holding_days=10)
    # 满仓账户(10 万本金/10 万单笔), 让持仓浮亏完整反映到账户净值
    res = Backtester(initial_capital=100_000.0, cash_per_trade=100_000.0).run(
        [sig], {("X", "CN"): bars}
    )
    assert res.trades[0].pnl > 0, "最终为盈利"
    assert res.metrics["max_drawdown"] > 0.1, "持仓期浮亏应计入 MDD"


def test_final_equity_equals_initial_plus_realized_pnl():
    """逐日盯市曲线终点 = 期初资金 + 已实现盈亏(口径自洽)。"""
    bars = [_bar(f"2026-01-{d:02d}", 10, 10.2, 9.8, 10 + (d % 3)) for d in range(1, 12)]
    sigs = [
        Signal("X", "CN", "2026-01-01", stop_loss=5, target_price=99, holding_days=4),
        Signal("X", "CN", "2026-01-05", stop_loss=5, target_price=99, holding_days=3),
    ]
    res = Backtester().run(sigs, {("X", "CN"): bars})
    realized = sum(t.pnl for t in res.trades)
    assert abs(res.equity_curve[-1] - (res.initial_capital + realized)) < 1e-6


def test_validate_equity_curve_contract():
    """契约校验: 长度不一致或日期缺失必须报错。"""
    with pytest.raises(ValueError):
        M.validate_equity_curve([100, 101], ["", "2026-01-02", "2026-01-03"])
    with pytest.raises(ValueError):
        M.validate_equity_curve([100, 101], ["", ""])
    M.validate_equity_curve([100, 101], ["", "2026-01-02"])
