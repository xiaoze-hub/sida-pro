"""W3.5/B5 结算路径 Decimal 测试: CostModel.fill / 平仓结算 / 可用现金 / 账户净值。"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.backtest.cost_model import CostConfig, CostModel  # noqa: E402
from src.core.money import q2, q4, to_dec  # noqa: E402

FREE = CostConfig(slippage_bps=0, commission_rate=0, min_commission=0,
                  stamp_duty_rate=0, transfer_fee_rate=0)


# ---------- CostModel.fill: Decimal 精确(对照测试内独立 Decimal 复算) ----------


def _expected_fill(side, price, qty, cfg):
    """测试内独立复算(Decimal), 不 import 产出代码 —— 交叉验证。"""
    price_d = to_dec(price)
    slip = price_d * to_dec(cfg.slippage_bps) / to_dec(10000)
    fill_price = price_d + slip if side == "buy" else max(to_dec(0), price_d - slip)
    gross = fill_price * qty
    commission = max(gross * to_dec(cfg.commission_rate), to_dec(cfg.min_commission))
    stamp = gross * to_dec(cfg.stamp_duty_rate) if side == "sell" else to_dec(0)
    transfer = gross * to_dec(cfg.transfer_fee_rate)
    slip_cost = abs(fill_price - price_d) * qty
    fees = commission + stamp + transfer
    cash = -(gross + fees) if side == "buy" else gross - fees
    return {
        "fill_price": float(q4_6(fill_price)), "gross": float(q4(gross)),
        "commission": float(q4(commission)), "stamp_duty": float(q4(stamp)),
        "transfer_fee": float(q4(transfer)), "slippage_cost": float(q4(slip_cost)),
        "explicit_fees": float(q4(fees)), "cash_delta": float(q4(cash)),
    }


def q4_6(d):
    from src.core.money import q6

    return q6(d)


def test_fill_matches_independent_decimal_across_matrix():
    cfg = CostConfig()  # 默认费率
    m = CostModel(cfg)
    for price, qty in [(8.2, 2500), (19.99, 333), (0.07, 10_000_000), (10.0, 100), (123.45, 700)]:
        for side in ("buy", "sell"):
            got = m.fill(side, price, qty)
            exp = _expected_fill(side, price, qty, cfg)
            for k, v in exp.items():
                assert getattr(got, k) == v, f"{side} {price}x{qty}.{k}: {getattr(got, k)} != {v}"


def test_fill_gross_decimal_exact_where_float_drifts():
    # 0.1×3 float = 0.30000000000000004 → Decimal 精确 0.3(零成本口径)
    f = CostModel(FREE).fill("buy", 0.1, 3)
    assert f.gross == 0.3 and f.cash_delta == -0.3
    # 0.07×10⁷ float = 700000.0000000001 → Decimal 700000 精确
    f2 = CostModel(FREE).fill("buy", 0.07, 10_000_000)
    assert f2.gross == 700000.0


def test_round_trip_pnl_drift_free():
    rt = CostModel().round_trip_pnl(0.1, 0.2, 1_000_000)
    invested, proceeds, pnl = to_dec(rt["invested"]), to_dec(rt["proceeds"]), to_dec(rt["pnl"])
    assert pnl == proceeds - invested  # Decimal 恒等, 无 float 残差
    assert rt["pnl"] == float(q4(pnl))


# ---------- compute_market_cash: float 伪影消除 ----------


def test_market_cash_no_float_artifact():
    from src.core.paper_trading_engine import compute_market_cash

    # 旧 float 版: 10000×0.3 = 3000.0000000000005
    assert compute_market_cash(10000, 0.3, 0.0, 0.0) == 3000.0
    exp = to_dec(10000) * to_dec(0.5) + to_dec(123.45) - to_dec(678.9)
    assert compute_market_cash(10000, 0.5, 123.45, 678.9) == float(q2(exp))


# ---------- _close_position: 平仓结算 Decimal(资金守恒口径) ----------


def test_close_position_settlement_exact():
    from src.core.paper_trading_engine import PaperTradingEngine
    from src.web.models import PaperTradingAccount, PaperTradingPosition, PaperTradingTrade

    class _DB:
        def add(self, obj):
            self.added = obj

    eng = PaperTradingEngine()
    account = PaperTradingAccount(
        initial_capital=1_000_000.0, current_capital=1_000_000.0,
        total_pnl=0.0, total_trades=0, winning_trades=0,
        peak_capital=0.0, max_drawdown_pct=0.0,
    )
    pos = PaperTradingPosition(
        stock_symbol="600519", stock_market="CN", stock_name="测试股",
        quantity=100, entry_price=10.0, status="open",
    )
    db = _DB()
    trade = eng._close_position(db, account, pos, 11.0, "stop_loss")

    # 测试内独立复算(默认费率: 佣金万2.5 最低5元/印花卖出万5/过户费十万1/滑点5bps)
    # 卖出滑点: 11×5bps = 0.0055 → fill 10.9945
    buy_cash = q4(-(to_dec(10.005) * 100 + max(to_dec(10.005) * 100 * to_dec(0.00025), to_dec(5))
                    + to_dec(10.005) * 100 * to_dec(0.00001)))
    sell_gross = to_dec(10.9945) * 100
    sell_fees = max(sell_gross * to_dec(0.00025), to_dec(5)) \
        + sell_gross * to_dec(0.0005) + sell_gross * to_dec(0.00001)
    sell_cash = q4(sell_gross - sell_fees)
    exp_pnl = q4(sell_cash + buy_cash)  # buy_cash 为负
    exp_pct = q2(exp_pnl / (-buy_cash) * to_dec(100))

    assert isinstance(trade, PaperTradingTrade)
    assert trade.pnl == float(exp_pnl) == 88.3793
    assert trade.pnl_pct == float(exp_pct) == 8.79
    assert account.total_pnl == 88.3793
    assert account.current_capital == 1_000_000.0 + float(sell_cash) == 1001093.8893
    assert pos.unrealized_pnl == 88.3793 and pos.status == "closed"
    assert account.total_trades == 1 and account.winning_trades == 1


# ---------- _update_account_metrics: 净值/回撤 Decimal ----------


def test_account_metrics_drawdown_decimal():
    from src.core.paper_trading_engine import PaperTradingEngine
    from src.web.models import PaperTradingAccount, PaperTradingPosition

    pos = PaperTradingPosition(quantity=100, entry_price=9.0, current_price=10.0)

    class _Q:
        def filter(self, *a):
            return self

        def all(self):
            return [pos]

    class _DB:
        def query(self, *a):
            return _Q()

    account = PaperTradingAccount(
        current_capital=10000.0, peak_capital=12000.0, max_drawdown_pct=0.0,
    )
    PaperTradingEngine()._update_account_metrics(_DB(), account)
    # equity = 10000 + 10×100 = 11000; drawdown = (12000-11000)/12000×100 = 8.333…% → 8.33
    assert account.peak_capital == 12000.0  # equity < peak 不上抬
    assert account.max_drawdown_pct == 8.33
